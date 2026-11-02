import argparse
import math
import os
import signal
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import uuid4

from .common import MODEL_ID, MODEL_REVISION, child, now, project_lock, read_json, require_t4, script_hash, validate_name, write_json
from .data import StoredDataset, collate_examples, dataset_manifest, prepare_data
from .common import checkpoint_state, validate_checkpoint


def parse_args():
    parser = argparse.ArgumentParser(description="CTH: training Qwen 0.5B, khusus T4.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--model-name", default="chanthecno-cb1")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--learning-rate", type=float, default=0.0001)
    parser.add_argument("--max-length", type=int, default=512, choices=[256, 512, 1024])
    parser.add_argument("--save-steps", type=int, default=25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dataset-manifest", help="Manifest lokal/Drive beku untuk run ini.")
    parser.add_argument("--resume-run", help="Nama run sebelumnya, bukan path checkpoint bebas.")
    return parser.parse_args()


def run(args):
    import torch
    import transformers
    import peft
    import accelerate
    import platform
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainerCallback, TrainingArguments, set_seed

    validate_name(args.model_name)
    if not math.isfinite(args.epochs) or not 0 < args.epochs <= 20:
        raise ValueError("Epoch harus lebih dari 0 dan maksimal 20.")
    if not math.isfinite(args.learning_rate) or not 0 < args.learning_rate <= 0.001:
        raise ValueError("Learning rate harus lebih dari 0 dan maksimal 0.001.")
    if not 1 <= args.save_steps <= 1000:
        raise ValueError("save-steps harus 1–1000.")
    gpu = require_t4()
    project = Path(args.project_dir).resolve()
    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        raise ValueError("Direktori dataset tidak ditemukan.")
    with project_lock(project):
        inputs = dataset_manifest(data_dir)
        settings = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "model_name": args.model_name,
                    "epochs": args.epochs, "learning_rate": args.learning_rate, "max_length": args.max_length,
                    "save_steps": args.save_steps, "seed": args.seed, "sources": inputs, "code_sha256": script_hash()}
        settings["dataset_manifest"] = read_json(args.dataset_manifest) if args.dataset_manifest else None
        resume = None
        if args.resume_run:
            directory = child(project / "training_results", args.resume_run)
            saved = read_json(directory / "run.json")
            if saved["settings"] != settings:
                raise ValueError("Data, kode, model, atau parameter berubah. Buat run baru; tidak melanjutkan checkpoint yang berbeda.")
            if read_json(directory / "status.json").get("status") == "completed":
                raise ValueError("Run sudah selesai. Gunakan ekspor atau buat run baru.")
            checkpoints = sorted((directory / "checkpoints").glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[-1]))
            complete = [p for p in checkpoints if (p / "cth-complete.json").exists()]
            if not complete:
                raise ValueError("Belum ada checkpoint lengkap untuk dilanjutkan.")
            resume = str(validate_checkpoint(complete[-1]))
        else:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
            directory = child(project / "training_results", run_id)
            directory.mkdir(parents=True, exist_ok=False)
            write_json(directory / "run.json", {"created_at": now(), "settings": settings, "gpu": gpu, "environment": {"python": platform.python_version(), "torch": torch.__version__, "transformers": transformers.__version__, "peft": peft.__version__, "accelerate": accelerate.__version__}})
        if args.resume_run:
            (directory / "stop.request").unlink(missing_ok=True)
        status_file = directory / "status.json"
        job_file = Path(os.environ["CTH_JOB_FILE"]) if os.environ.get("CTH_JOB_FILE") else None
        job_stop = Path(os.environ["CTH_STOP_FILE"]) if os.environ.get("CTH_STOP_FILE") else None
        def publish(data):
            write_json(status_file, data)
            if job_file:
                current = read_json(job_file)
                current.update(data)
                current["gpu_verified"] = True
                write_json(job_file, current)
        print(f"Run: {directory.name}", flush=True)
        print(f"GPU: {gpu['name']} | Model: {MODEL_ID}", flush=True)
        print("Folder kerja harus disinkronkan ke Drive oleh integrasi CTH; skrip ini tidak mengunggah ke Drive.", flush=True)
        stopped = {"value": False}
        previous_handlers = {}
        def stop_handler(signum, frame):
            stopped["value"] = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, stop_handler)
        try:
            publish({"status": "preparing", "updated_at": now(), "run_id": directory.name})
            set_seed(args.seed)
            tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION, trust_remote_code=False)
            tokenizer.pad_token = tokenizer.eos_token
            tokenizer.padding_side = "right"
            database = directory / "dataset.sqlite"
            if not args.resume_run:
                selected = (settings['dataset_manifest'] or {}).get('datasets', [])
                source_names = {f'{index:04d}.txt': f"{item['name']} (Drive: {item['file_id']})" for index, item in enumerate(selected)}
                source_modes = {f'{index:04d}.txt': item.get('source_mode', 'filtered') for index, item in enumerate(selected)}
                stats = prepare_data(data_dir, inputs, tokenizer, args.max_length, database, args.seed, source_names, source_modes)
                if dataset_manifest(data_dir) != inputs:
                    raise ValueError("Dataset berubah selama persiapan. Buat run baru dari data yang tetap.")
                write_json(directory / "dataset_stats.json", stats)
                write_json(directory / "source-index-version.json", {"version": 2, "source_analysis": stats["source_analysis"]})
                publish({"status": "preparing", "updated_at": now(), "run_id": directory.name, "dataset_stats": stats})
            model = AutoModelForCausalLM.from_pretrained(MODEL_ID, revision=MODEL_REVISION,
                torch_dtype=torch.float16, device_map={"": 0}, trust_remote_code=False, use_safetensors=True, attn_implementation="eager")
            model.config.use_cache = False
            # LoRA FP16: satu model kecil, tanpa teacher atau model pembuat data.
            model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05,
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
                bias="none", task_type="CAUSAL_LM"))
            model.enable_input_require_grads()
            model.print_trainable_parameters()
            class Progress(TrainerCallback):
                def on_log(self, args, state, control, logs=None, **kwargs):
                    metrics = {key: value for key, value in (logs or {}).items() if isinstance(value, (int, float)) and math.isfinite(value)}
                    publish({"status": "training", "updated_at": now(), "run_id": directory.name,
                        "step": state.global_step, "max_steps": state.max_steps, "epoch": state.epoch, "metrics": metrics})
                def on_step_end(self, args, state, control, **kwargs):
                    if (directory / "stop.request").exists() or (job_stop and job_stop.exists()):
                        stopped["value"] = True
                    if stopped["value"]:
                        control.should_save = True
                        control.should_training_stop = True
                    return control
                def on_save(self, args, state, control, **kwargs):
                    checkpoint = directory / "checkpoints" / f"checkpoint-{state.global_step}"
                    write_json(checkpoint / "cth-complete.json", {"step": state.global_step, "saved_at": now(), "files": checkpoint_state(checkpoint)})
            training_args = TrainingArguments(output_dir=str(directory / "checkpoints"), num_train_epochs=args.epochs,
                learning_rate=args.learning_rate, per_device_train_batch_size=1, per_device_eval_batch_size=1,
                gradient_accumulation_steps=8, gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
                fp16=True, bf16=False, optim="adamw_torch", warmup_ratio=0.05, lr_scheduler_type="cosine",
                weight_decay=0.01, max_grad_norm=1.0, logging_steps=1, save_strategy="steps", save_steps=args.save_steps,
                eval_strategy="epoch", save_total_limit=3, report_to="none", seed=args.seed, data_seed=args.seed,
                dataloader_num_workers=0, remove_unused_columns=False, label_names=["labels"], save_safetensors=True)
            trainer = Trainer(model=model, args=training_args, processing_class=tokenizer,
                train_dataset=StoredDataset(database, "train"), eval_dataset=StoredDataset(database, "eval"),
                data_collator=partial(collate_examples, pad_token_id=tokenizer.pad_token_id), callbacks=[Progress()])
            trainer.train(resume_from_checkpoint=resume)
            if stopped["value"]:
                publish({"status": "stopped", "updated_at": now(), "run_id": directory.name,
                    "step": trainer.state.global_step, "message": "Dihentikan setelah optimizer step; lanjutkan dari checkpoint lengkap."})
                print("Training dihentikan. Checkpoint dipertahankan.", flush=True)
                return
            metrics = trainer.evaluate()
            safe_metrics = {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and math.isfinite(v)}
            adapter = directory / "adapter"
            trainer.save_model(str(adapter))
            tokenizer.save_pretrained(str(adapter))
            write_json(directory / "evaluation.json", safe_metrics)
            write_json(directory / "complete.json", {"completed_at": now(), "step": trainer.state.global_step})
            publish({"status": "completed", "updated_at": now(), "run_id": directory.name,
                "step": trainer.state.global_step, "adapter": "adapter", "metrics": safe_metrics})
            print(f"Training selesai. Adapter: {adapter}", flush=True)
            print("Eval loss bukan bukti jawaban selalu benar. Uji pertanyaan nyata sebelum dipakai.", flush=True)
        except BaseException as error:
            publish({"status": "failed", "updated_at": now(), "run_id": directory.name,
                "error_type": type(error).__name__, "message": str(error)[:400] if isinstance(error, ValueError) else "Pekerjaan gagal. Checkpoint lengkap tetap dipertahankan; periksa log runtime."})
            raise
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def main():
    run(parse_args())

if __name__ == "__main__":
    main()
