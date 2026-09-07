"""
Stock Indicators Calculation Modules
تقسيم المؤشرات إلى 4 وحدات متخصصة
"""

from .calculate_rsi_indicators import *
from .calculate_the_number_indicators import *
from .calculate_stamp_indicators import *
from .calculate_trend_screener_indicators import *
from .indicators_data_service import IndicatorsDataService

import importlib.util
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent

# Sukuk&Bonds dynamic export
_sukuk_path = _scripts_dir / "Sukuk&Bonds.py"
if _sukuk_path.exists():
    _spec = importlib.util.spec_from_file_location("scripts.Sukuk_and_Bonds", str(_sukuk_path))
    Sukuk_and_Bonds = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(Sukuk_and_Bonds)
    import sys
    sys.modules["scripts.Sukuk_and_Bonds"] = Sukuk_and_Bonds

# SAMA&GaStat dynamic export
_sama_path = _scripts_dir / "SAMA&GaStat.py"
if _sama_path.exists():
    _spec = importlib.util.spec_from_file_location("scripts.SAMA_and_GaStat", str(_sama_path))
    SAMA_and_GaStat = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(SAMA_and_GaStat)
    import sys
    sys.modules["scripts.SAMA_and_GaStat"] = SAMA_and_GaStat