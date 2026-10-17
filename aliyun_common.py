"""公共工具：读取 config.yaml、构造客户端、调用 OpenAPI。绝不打印 AK/SK。"""
import json
import warnings
from pathlib import Path

import yaml
from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
STATE_DIR = ROOT / "state"
TOP_KEYS = ("region_id", "traffic_threshold_gb", "check_interval_seconds", "instance_type", "zone_id")
DEFAULTS = {"region_id": "cn-hongkong", "traffic_threshold_gb": 180, "check_interval_seconds": 60}

ApiError = (ClientException, ServerException)


def load_config():
    """返回 (defaults, accounts)。accounts 是 {别名: 账号配置}，每个账号带 key（别名）和可选 uid。

    支持三种写法：
      最简：顶层直接 access_key_id / access_key_secret（单账号，别名 default）
      多账号：accounts 列表，每项 name + AK/SK，可选 uid / region_id
      旧版：accounts 是以 UID 为键的映射，默认值在 defaults 下
    """
    if not CONFIG.exists():
        raise SystemExit("找不到 config.yaml，先执行: cp config.yaml.template config.yaml")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    defaults = dict(DEFAULTS)
    defaults.update({k: v for k, v in (cfg.get("defaults") or {}).items() if v is not None})
    defaults.update({k: cfg[k] for k in TOP_KEYS if cfg.get(k) is not None})
    raw = cfg.get("accounts") or []
    if not raw and cfg.get("access_key_id"):          # 最简写法：顶层直接写一对 AK/SK
        raw = [{k: cfg[k] for k in ("name", "access_key_id", "access_key_secret", "uid", "instances") if cfg.get(k) is not None}]
    accounts = {}
    if isinstance(raw, dict):
        for uid, acc in raw.items():
            acc = dict(acc or {})
            acc.setdefault("uid", str(uid))
            acc.setdefault("name", str(uid))
            accounts[str(acc["name"])] = acc
    else:
        for i, acc in enumerate(raw):
            acc = dict(acc or {})
            acc.setdefault("name", "default" if i == 0 else f"account{i + 1}")
            if acc.get("uid") is not None:
                acc["uid"] = str(acc["uid"])
            accounts[str(acc["name"])] = acc
    for key, acc in accounts.items():
        acc["key"] = key
    return defaults, accounts


def make_client(acc, defaults):
    region = acc.get("region_id") or defaults.get("region_id") or "cn-hongkong"
    return AcsClient(acc["access_key_id"], acc["access_key_secret"], region), region


def call(client, domain, version, action, **params):
    req = CommonRequest()
    req.set_domain(domain)
    req.set_version(version)
    req.set_action_name(action)
    req.set_method("POST")
    req.set_accept_format("json")
    req.set_protocol_type("https")
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        req.add_query_param(key, value)
    return json.loads(client.do_action_with_exception(req))


def ecs(client, action, **p):
    return call(client, "ecs.aliyuncs.com", "2014-05-26", action, **p)


def vpc(client, action, **p):
    return call(client, "vpc.aliyuncs.com", "2016-04-28", action, **p)


def err_str(e):
    try:
        return f"{e.get_error_code()}: {e.get_error_msg()}"
    except Exception:
        return str(e)


def _key_uid(acc):
    return (acc["key"], acc.get("uid")) if isinstance(acc, dict) else (acc, None)


def load_state(acc):
    """state/<别名>.json；旧版按 UID 命名的文件会自动改名迁移。"""
    key, uid = _key_uid(acc)
    STATE_DIR.mkdir(exist_ok=True)
    p = STATE_DIR / f"{key}.json"
    legacy = STATE_DIR / f"{uid}.json" if uid else None
    if not p.exists() and legacy and legacy.exists():
        legacy.rename(p)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(acc, state):
    key, _ = _key_uid(acc)
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{key}.json").write_text(json.dumps(state, indent=2, ensure_ascii=False))
