"""
export_qlora_dataset.py
data/{train,validation,test,final_test}_set.json → Colab QLoRA 파인튜닝용 JSONL 변환.

train_set.json + validation_set.json → data/qlora/train.jsonl (SFT 학습용)
test_set.json + final_test_set.json  → data/qlora/eval.jsonl  (홀드아웃 평가용, 학습에 안 씀)

각 레코드는 Qwen chat template과 호환되는 {"messages": [...]}  형식이며,
instruction은 experiments/run_l2_accuracy.py의 CLASSIFY_PROMPT를 그대로 확장한 것이라
같은 카테고리 taxonomy·평가 방식(Category Accuracy/Action Accuracy)으로 Groq 결과와
직접 비교 가능하다.
"""
import json
from pathlib import Path

DATA_DIR = Path("data")
OUT_DIR = DATA_DIR / "qlora"

INSTRUCTION = """You are a log classification expert for MLOps systems.
Given an error log, output EXACTLY this JSON format and nothing else:
{{"category": "<CATEGORY>", "action": "<ACTION>", "target_process": "<PROCESS or null>"}}

Categories: Out_Of_Memory, Memory_Leak, CPU_Overload, DB_Connection, DB_Timeout, DB_Deadlock, Network_Timeout, Network_Unreachable, Permission_Denied, Configuration_Error, Auth_Error, Path_Not_Found, Disk_Full, Process_Crash, Port_Conflict, Unknown

Actions: clear_memory, restart_service, kill_process, escalate_to_human, execute_llm_command, execute_rule_command, alert_only

Error log:
{log}

JSON:"""


def _load(path: Path) -> list[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    return obj["data"] if isinstance(obj, dict) else obj


def _to_sample(item: dict) -> dict:
    user_msg = INSTRUCTION.format(log=item["log_text"][:600])
    completion = json.dumps(
        {
            "category": item["error_category"],
            "action": item["action_type"],
            "target_process": item.get("target_process"),
        },
        ensure_ascii=False,
    )
    return {"messages": [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": completion},
    ]}


def _write_jsonl(items: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(_to_sample(item), ensure_ascii=False) + "\n")


def main() -> None:
    train = _load(DATA_DIR / "train_set.json") + _load(DATA_DIR / "validation_set.json")
    eval_ = _load(DATA_DIR / "test_set.json") + _load(DATA_DIR / "final_test_set.json")

    _write_jsonl(train, OUT_DIR / "train.jsonl")
    _write_jsonl(eval_, OUT_DIR / "eval.jsonl")

    print(f"train.jsonl: {len(train)}건 → {OUT_DIR / 'train.jsonl'}")
    print(f"eval.jsonl : {len(eval_)}건 → {OUT_DIR / 'eval.jsonl'}")


if __name__ == "__main__":
    main()
