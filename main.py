#!/usr/bin/env python3
"""Ядро «Сигнал.kg» одной командой (то же, что python -m covermap):

    python main.py --measurements data.csv --towers towers.csv --candidates candidates.csv

Пишет result.json, report.json и map.html в каталог --out (по умолчанию out/).
"""
import sys

from covermap.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
