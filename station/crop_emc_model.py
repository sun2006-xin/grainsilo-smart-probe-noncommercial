"""Source-bounded reference EMC calculations for the PC Station."""
import math


TABLE_SOURCE = (
    "https://files.ontario.ca/omafra-natural-air-grain-drying-20-043-en-2023-05-16.pdf"
)
PADDY_SOURCE = (
    "https://doi.org/10.1590/1809-4430-Eng.Agric.v39n4p524-532/2019"
)

_TEMP_GRID = (0, 5, 10, 15, 20, 25, 30)
_RH_GRID = (50, 60, 70, 80, 90)
_TABLES = {
    ("corn", None): (
        (13.7, 15.1, 16.6, 18.4, 21.3),
        (13.1, 14.4, 15.9, 17.8, 20.7),
        (12.5, 13.8, 15.4, 17.3, 20.2),
        (11.9, 13.3, 14.9, 16.8, 19.8),
        (11.5, 12.8, 14.4, 16.4, 19.4),
        (11.0, 12.4, 14.0, 16.0, 19.0),
        (10.6, 12.0, 13.6, 15.6, 18.7),
    ),
    ("wheat", "soft"): (
        (12.5, 13.5, 14.6, 16.1, 18.2),
        (12.1, 13.1, 14.2, 15.7, 17.9),
        (11.7, 12.7, 13.9, 15.3, 17.5),
        (11.4, 12.4, 13.5, 15.0, 17.2),
        (11.1, 12.1, 13.2, 14.7, 17.0),
        (10.8, 11.8, 13.0, 14.4, 16.7),
        (10.5, 11.5, 12.7, 14.2, 16.5),
    ),
    ("wheat", "hard"): (
        (13.3, 14.6, 16.1, 17.9, 20.7),
        (12.9, 14.2, 15.7, 17.5, 20.3),
        (12.6, 13.9, 15.3, 17.2, 20.0),
        (12.2, 13.5, 15.0, 16.9, 19.7),
        (11.9, 13.2, 14.7, 16.6, 19.5),
        (11.6, 12.9, 14.4, 16.3, 19.2),
        (11.3, 12.6, 14.2, 16.1, 19.0),
    ),
}
_PADDY = {
    "adsorption": (35.93673, 6.08728, 50.48329),
    "desorption": (37.41332, 6.23300, 49.64200),
}


def _bracket(grid, value):
    for index in range(len(grid) - 1):
        if grid[index] <= value <= grid[index + 1]:
            span = grid[index + 1] - grid[index]
            return index, index + 1, (value - grid[index]) / span
    raise ValueError("输入超出来源数据范围")


def _table_emc(table, temp_c, rh_pct):
    ti, tj, tf = _bracket(_TEMP_GRID, temp_c)
    ri, rj, rf = _bracket(_RH_GRID, rh_pct)
    at_t0 = table[ti][ri] + rf * (table[ti][rj] - table[ti][ri])
    at_t1 = table[tj][ri] + rf * (table[tj][rj] - table[tj][ri])
    return at_t0 + tf * (at_t1 - at_t0)


def _paddy_emc(temp_c, rh_pct, path):
    a, b, c = _PADDY[path]
    argument = -(temp_c + c) * math.log(rh_pct / 100.0)
    dry_basis = a - b * math.log(argument)
    if not math.isfinite(dry_basis) or dry_basis <= 0:
        raise ValueError("模型在此输入下未给出有效结果")
    wet_basis = 100.0 * dry_basis / (100.0 + dry_basis)
    return {"path": path, "dry_basis_pct": dry_basis,
            "wet_basis_pct": wet_basis}


def calculate_emc(crop_type, temp_c, rh_pct, wheat_type="soft",
                  paddy_path="both"):
    """Calculate crop EMC from cited tables/fit; never extrapolate."""
    if crop_type not in ("paddy", "wheat", "corn"):
        raise ValueError("作物必须为 paddy、wheat 或 corn")
    if isinstance(temp_c, bool) or isinstance(rh_pct, bool):
        raise ValueError("温度和相对湿度必须是有效数字")
    try:
        temp_c, rh_pct = float(temp_c), float(rh_pct)
    except (TypeError, ValueError):
        raise ValueError("温度和相对湿度必须是有效数字") from None
    if not math.isfinite(temp_c) or not math.isfinite(rh_pct):
        raise ValueError("温度和相对湿度必须是有限数字")

    if crop_type == "paddy":
        if not 10 <= temp_c <= 50 or not 11 <= rh_pct <= 76:
            raise ValueError("稻谷文献范围为 10–50°C、11–76%RH")
        if paddy_path not in ("both", "adsorption", "desorption"):
            raise ValueError("稻谷路径必须为 both、adsorption 或 desorption")
        paths = ("adsorption", "desorption") if paddy_path == "both" else (paddy_path,)
        estimates = [_paddy_emc(temp_c, rh_pct, path) for path in paths]
        return {"crop_type": crop_type, "model": "Chung-Pfost（Urucuia 稻谷文献拟合）",
                "basis": "wet", "estimates": estimates,
                "temperature_range_c": [10, 50], "rh_range_pct": [11, 76],
                "source_url": PADDY_SOURCE}

    if crop_type == "wheat" and wheat_type not in ("soft", "hard"):
        raise ValueError("小麦必须选择 soft 或 hard")
    if not 0 <= temp_c <= 30 or not 50 <= rh_pct <= 90:
        raise ValueError("玉米/小麦表格范围为 0–30°C、50–90%RH")
    table = _TABLES[(crop_type, wheat_type if crop_type == "wheat" else None)]
    value = _table_emc(table, temp_c, rh_pct)
    curve = "软麦" if wheat_type == "soft" else "硬麦"
    model = "Modified Chung-Pfost 来源表格（湿基）"
    if crop_type == "wheat":
        model += " - " + curve
    return {"crop_type": crop_type, "model": model, "basis": "wet",
            "estimates": [{"path": "table", "wet_basis_pct": value}],
            "temperature_range_c": [0, 30], "rh_range_pct": [50, 90],
            "source_url": TABLE_SOURCE}
