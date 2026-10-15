# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("smyvy_parser", _ROOT / "parser.py")
smyvy_parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smyvy_parser)


def test_parse_kma_scientific():
    val, below = smyvy_parser.parse_kma("6,4 х 10*3")
    assert val == 6400.0
    assert below is False


def test_parse_kma_below_detection():
    val, below = smyvy_parser.parse_kma("менее 1,5 х 10*2")
    assert val == 150.0
    assert below is True


def test_parse_kma_empty():
    assert smyvy_parser.parse_kma("-") == (None, None)
    assert smyvy_parser.parse_kma(None) == (None, None)


def test_classify_kma_exceed():
    assert smyvy_parser.classify("КМАФАнМ", "2,0 х 10*3") == "превышение"


def test_classify_kma_norm():
    assert smyvy_parser.classify("КМАФАнМ", "менее 1,5 х 10*2") == "норма"


def test_classify_detection():
    assert smyvy_parser.classify("БГКП", "обнаружено") == "несоответствие"


def test_classify_not_tested():
    assert smyvy_parser.classify("БГКП", "") == "не тестировали"


def test_clean_loc():
    assert smyvy_parser._clean_loc("точка\n  с переносом") == "точка с переносом"
