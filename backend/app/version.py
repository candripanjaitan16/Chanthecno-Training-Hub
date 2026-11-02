"""Single source of the CTH application/installer version."""
import re

VERSION = '1.1.0'
REPOSITORY = 'candripanjaitan16/Chanthecno-Training-Hub'


def stable_version(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Versi rilis tidak didukung.')
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[A-Za-z0-9.-]+)?', value)
    if not match:
        raise ValueError('Gunakan versi stabil major.minor.patch.')
    return tuple(int(part) for part in match.groups())
