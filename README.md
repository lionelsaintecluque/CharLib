# CharLib: An open-source standard cell library characterizer

- 🔩 Supports combinational and sequential cells
- 📈 Plots timing and I/O voltages
- 🧑‍💻 Easy-to-use, with YAML-based configuration
- 🐍 Implemented in Python 3 with a modified PySpice backend
- 🌶️ Compatible with ngspice and Xyce

## Getting Started

> **This checkout is the `integration` branch** (CharLib 2.1.0): upstream `database` merged with
> the private `nebula` Liberty fixes and the session-engine rework. It is a local, unpublished
> branch — install it **from this working copy**, not from the upstream GitHub tag.

Requirements:
- Python >= 3.12
- `libngspice.so` — ngspice built **with `--with-ngshared`** (CharLib drives ngspice through
  PySpice as a shared library, not as a subprocess). Debian/Ubuntu: `apt install libngspice0-dev`.
  Fedora has no such package: build it from source, see `~/sources/MEMO_INSTALL_CHARLIB.md`, step 2.
- Network access at install time (the patched PySpice is pulled from git by `pyproject.toml`).

```
# From the root of this checkout
git rev-parse --abbrev-ref HEAD          # -> integration
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
# Tell PySpice where libngspice.so lives (bare name with {} — validated on Python 3.13)
export NGSPICE_LIBRARY_PATH="ngspice{}"
export LD_LIBRARY_PATH="$HOME/local/lib"  # directory containing libngspice.so, if not system-wide
# Characterize some cells
cd test/pdks/osu350
charlib run osu350.yml -j4 -f AND
```

Branch-specific notes live next to this file: `DECISIONS.md`, `KNOWN-BUGS.md`, `WISHLIST.md`.

## Introduction
CharLib is an open-source standard cell library characterizer. The current version supports NLDM
timing characterization of combinational and sequential cells.

See the [documentation](https://stineje.github.io/CharLib/) for more details, including:
- [Installation instructions](https://stineje.github.io/CharLib/chapters/02_installation.html)
- [Detailed usage information](https://stineje.github.io/CharLib/chapters/03_user_guide.html)
- [Information for developers & contributors](https://stineje.github.io/CharLib/chapters/06_contributing.html)

We've also published a [video tutorial](https://youtu.be/QYgwKUkUTOc?si=QH0ajlIu4vUQsxAa) that
walks through the process of getting started with CharLib, from installation to characterizing a
PDK.

## Troubleshooting
For upstream CharLib behaviour, [open a new issue](https://github.com/stineje/CharLib/issues/new/choose).
Anything specific to the `integration` branch (session engine, measurement DB, c2q arcs, Liberty
fixes) is not upstream's concern: check `KNOWN-BUGS.md` first, then ask the branch owner.

## Citing
If you use this work in your research, please cite as follows:

```bibtex
@inproceedings{mellor_charlib_2024,
    title = {{CharLib}: {An} {Open} {Source} {Standard} {Cell} {Library} {Characterizer}},
    shorttitle = {{CharLib}},
    url = {https://ieeexplore.ieee.org/document/10658687},
    doi = {10.1109/MWSCAS60917.2024.10658687},
    booktitle = {2024 {IEEE} 67th {International} {Midwest} {Symposium} on {Circuits} and {Systems} ({MWSCAS})},
    author = {Mellor, Marcus and Stine, James E.},
    month = aug,
    year = {2024},
    note = {ISSN: 1558-3899},
    keywords = {Accuracy, Circuits and systems, Design tools, Libraries, Micrometers, Process control, Silicon},
    pages = {277--281},
}
```
