"""业务常量，取值依据 docs/资料口径.md。"""
from decimal import Decimal

TOLERANCE_G = Decimal("0.2")  # 称量容差：超差待核，不自动生成损耗
STORE_HOLDER = "store"  # 所有根容器最初保管方
MOVE_ACTIONS = ("issue", "return", "transfer")  # 只变保管方，不减全区实物总量
USE_KINDS = ("use", "evaporate", "destroy")
# 核销确认资格：仅 confirmed use、documented evaporate、witnessed destroy
ELIGIBLE = {("use", "confirmed"), ("evaporate", "documented"), ("destroy", "witnessed")}
PENDING_EVIDENCE = "pending"  # 申报未足证：不同时按确认损失和可用余量计两次
# 差异分类用的资料表分组
EVENT_TABLES = ("packs", "decants", "movements", "uses", "weighings")  # 新增实物动作
CORRECTION_TABLES = ("density_corrections", "specifications")  # 历史资料补正
COST_TABLES = ("materials",)  # 成本资料变化
