#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Обновляет встроенную историю в smyvy.html из Excel-журнала.

Использование:
    python refresh_embedded_history.py
    python refresh_embedded_history.py "07. Результаты смывов 2026 (1).xlsx"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

from parser import INDICATORS, parse_workbook


ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "smyvy.html"
DEFAULT_GLOB = "07. Результаты смывов 2026*.xlsx"
IND_ORDER = list(INDICATORS.keys())
LIMIT = 1000.0


def pick_source() -> Path:
    if len(sys.argv) > 1:
        return (ROOT / sys.argv[1]).resolve()
    files = sorted(ROOT.glob(DEFAULT_GLOB), key=lambda p: (p.name.count("("), p.name.lower()))
    if not files:
        raise SystemExit("Excel-файл не найден.")
    return files[-1]


def build_payload(df: pd.DataFrame) -> tuple[dict, dict]:
    cehs = list(dict.fromkeys(df["цех"].dropna().astype(str)))
    ceh_index = {name: i for i, name in enumerate(cehs)}

    probes = []
    for (ceh, date, point), group in df.groupby(["цех", "дата", "точка"], sort=False):
        chars = []
        kma = None
        for ind in IND_ORDER:
            row = group[group["показатель"] == ind]
            if row.empty:
                chars.append(".")
                continue
            status = row.iloc[0]["статус"]
            raw = str(row.iloc[0]["значение"] or "")
            if ind == "КМАФАнМ":
                if status == "не тестировали":
                    chars.append(".")
                elif status == "превышение":
                    chars.append("o")
                    kv = row.iloc[0]["кмафанм_кое"]
                    if pd.notna(kv):
                        kma = int(kv)
                elif "менее" in raw.lower():
                    chars.append("n")
                else:
                    chars.append("e")
                    kv = row.iloc[0]["кмафанм_кое"]
                    if pd.notna(kv):
                        kma = int(kv)
            else:
                chars.append("." if status == "не тестировали" else ("x" if status == "несоответствие" else "n"))

        probe = {
            "d": date.strftime("%Y-%m-%d") if pd.notna(date) else None,
            "c": ceh_index[str(ceh)],
            "p": str(point),
            "s": "".join(chars),
        }
        if kma is not None:
            probe["k"] = kma
        probes.append(probe)

    ref_points = {}
    ref_inds = {}
    for ceh in cehs:
        g = df[df["цех"] == ceh]
        ref_points[ceh] = list(dict.fromkeys(g["точка"].dropna().astype(str)))
        used = []
        for ind in IND_ORDER:
            ig = g[g["показатель"] == ind]
            if not ig.empty and (ig["статус"] != "не тестировали").any():
                used.append(ind)
        ref_inds[ceh] = used or IND_ORDER[:]

    hist = {"cehs": cehs, "inds": IND_ORDER, "limit": LIMIT, "probes": probes}
    ref = {"points": ref_points, "inds": ref_inds}
    return hist, ref


def replace_block(html: str, hist: dict, ref: dict) -> str:
    hist_json = json.dumps(hist, ensure_ascii=False, separators=(",", ":"))
    ref_json = json.dumps(ref, ensure_ascii=False, separators=(",", ":"))

    html, n1 = re.subn(
        r"const HIST = .*?;\s*const REF = ",
        f"const HIST = {hist_json};\nconst REF = ",
        html,
        count=1,
        flags=re.S,
    )
    if n1 != 1:
        raise RuntimeError("Не удалось найти блок HIST в smyvy.html")

    html, n2 = re.subn(
        r"const REF = .*?;\s*const IND = HIST\.inds;",
        f"const REF = {ref_json};\nconst IND = HIST.inds;",
        html,
        count=1,
        flags=re.S,
    )
    if n2 != 1:
        raise RuntimeError("Не удалось найти блок REF в smyvy.html")

    return html


def main() -> None:
    src = pick_source()
    df = parse_workbook(str(src))
    hist, ref = build_payload(df)

    html = HTML_PATH.read_text(encoding="utf-8")
    updated = replace_block(html, hist, ref)
    HTML_PATH.write_text(updated, encoding="utf-8")

    probe_count = df.groupby(["цех", "дата", "точка"]).ngroups
    print(f"Источник: {src.name}")
    print(f"Цехов: {len(hist['cehs'])}")
    print(f"Точек: {sum(len(v) for v in ref['points'].values())}")
    print(f"Проб: {probe_count}")
    print("smyvy.html обновлён")


if __name__ == "__main__":
    main()
