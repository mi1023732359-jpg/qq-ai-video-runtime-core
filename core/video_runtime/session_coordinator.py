# -*- coding: utf-8 -*-
"""Session Coordinator：视频会话与消息发送的唯一裁决者。

专项目标（用户硬性规则 五.1–10 的落点）：

  1. 来电先查资格（管理员开关 / 黑名单 / 免打扰 / 联系人白名单）
  2. 只有授权通过才允许自动接听
  3. 通话期间禁止普通主动消息、自动回复、社交操作
  4. 普通消息照常接收并记录，只有**发送**进互斥协调器
  5. 视频会话独立 session_id / history / summary / facts / archive / world
  6. 视频内容不污染普通聊天记忆（物理分目录，两套命名空间）
  7. 通话结束释放租约，恢复发送资格
  8. 超时 / 断线 / 崩溃 / 强制挂断都要释放锁
  9. 所有操作幂等（重复接听、重复挂断、重复回复无副作用）
 10. 管理员最高优先级（可强制拒接 / 挂断 / 关闭自动接听）

会话拓扑（account_state.py 探测）：本机是 `same_process_injected`（NapCat 注入官方
QQNT，共享一个登录会话），所以这里**不做账号级互斥**，只做业务层互斥；若拓扑变成
`independent_local_client` / `remote_or_container_client`，`requires_account_handover`
会为 True，上层必须先完成账号交接再调用接听（本模块直接拒绝，不假装成功）。

零生产依赖：不 import QQAI / NapCat / ADI，只写自己 store_root 下的文件。

自测：python video_runtime\\test_session_coordinator.py
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 会话状态
IDLE = "IDLE"
RINGING = "RINGING"
AUTHORIZING = "AUTHORIZING"
IN_CALL = "IN_CALL"
ENDED = "ENDED"
REJECTED = "REJECTED"

# 结束原因（也用于拒接原因之外的收尾归因）
RING_TIMEOUT = "ring_timeout"
CALL_TIMEOUT = "call_timeout"
DISCONNECTED = "disconnected"
CRASHED = "crashed"
FORCED = "forced_hangup"
NORMAL = "normal"

# 发送租约的持有者：视频会话自己永远可以发，其他一律排队/拒绝
VIDEO_OWNER = "video"


class Policy:
    """接听授权策略。默认从严：没进白名单就不接。"""

    def __init__(self, allowlist=(), blacklist=(), admin_uins=(), dnd=None,
                 auto_answer=True, require_allowlist=True):
        self.allowlist = {str(u) for u in allowlist}
        self.blacklist = {str(u) for u in blacklist}
        self.admin_uins = {str(u) for u in admin_uins}
        self.dnd = dnd                     # ((start_hour, end_hour), ...) 24h 制，可跨零点
        self.auto_answer = auto_answer
        self.require_allowlist = require_allowlist

    def in_dnd(self, when):
        if not self.dnd:
            return False
        hour = time.localtime(when).tm_hour
        for start, end in self.dnd:
            if start == end:
                continue
            if start < end and start <= hour < end:
                return True
            if start > end and (hour >= start or hour < end):   # 跨零点
                return True
        return False

    def authorize(self, caller, when=None):
        """返回 (allowed, reason)。顺序即优先级，管理员在最后一步提权。"""
        caller = str(caller)
        when = time.time() if when is None else when
        if not self.auto_answer:
            return False, "auto_answer_disabled"
        if caller in self.blacklist:
            return False, "blacklisted"
        if caller in self.admin_uins:
            return True, "admin"           # 管理员最高优先级：可越过白名单与免打扰
        if self.in_dnd(when):
            return False, "do_not_disturb"
        if self.require_allowlist and self.allowlist and caller not in self.allowlist:
            return False, "not_in_allowlist"
        if self.require_allowlist and not self.allowlist:
            return False, "empty_allowlist"
        return True, "authorized"


class Store:
    """会话存储：视频与普通聊天物理隔离，各自独立命名空间。"""

    KINDS = ("history", "summary", "facts", "archive", "world")

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def namespace(self, scope, key):
        """scope 只能是 video / chat，key 是 session_id 或 chat_id。"""
        if scope not in ("video", "chat"):
            raise ValueError("scope must be video|chat, got %r" % (scope,))
        safe = "".join(c for c in str(key) if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("bad key %r" % (key,))
        path = self.root / "sessions" / scope / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def append(self, scope, key, kind, record):
        if kind not in self.KINDS:
            raise ValueError("unknown kind %r" % (kind,))
        folder = self.namespace(scope, key)
        with open(folder / (kind + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def put(self, scope, key, kind, value):
        if kind not in self.KINDS:
            raise ValueError("unknown kind %r" % (kind,))
        folder = self.namespace(scope, key)
        tmp = folder / (kind + ".json.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, folder / (kind + ".json"))

    def load(self, scope, key, kind):
        folder = self.namespace(scope, key)
        f = folder / (kind + ".json")
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        f = folder / (kind + ".jsonl")
        if f.exists():
            return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        return None

    def has(self, scope, key, kind):
        folder = self.root / "sessions" / scope / str(key)
        return (folder / (kind + ".jsonl")).exists() or (folder / (kind + ".json")).exists()


class SessionCoordinator:
    MAX_CALL_MEMORY = 200      # 幂等缓存上限：长跑进程也不会无限涨

    def __init__(self, store_root=None, log_path=None, policy=None,
                 clock=time.monotonic, wall=time.time,
                 ring_timeout=30.0, max_call_seconds=1800.0, heartbeat_timeout=25.0,
                 session_topology="unknown", auto_recover=True):
        self.clock = clock
        self.wall = wall
        self.ring_timeout = ring_timeout
        self.max_call_seconds = max_call_seconds
        self.heartbeat_timeout = heartbeat_timeout
        self.store = Store(store_root or (ROOT / "logs" / "coordinator_store"))
        self.log_path = Path(log_path) if log_path else (ROOT / "logs" / "coordinator.jsonl")
        self.policy = policy or Policy()
        self.session_topology = session_topology
        self.requires_account_handover = session_topology in (
            "independent_local_client", "remote_or_container_client")
        self._lock = threading.RLock()
        self._calls = {}           # call_id -> 首次判定结果（幂等依据）
        self._state = IDLE
        self._call = None          # 当前通话
        self._lease = None         # {"owner","call_id","since"}
        self._state_file = Path(self.store.root) / "coordinator_state.json"
        self._owner_pid = os.getpid()
        self._recovered = False
        if auto_recover:
            self.recover_from_crash()

    # ---------- 日志与持久化 ----------
    def _log(self, event, **detail):
        rec = {"ts": round(self.wall(), 3), "mono": round(self.clock(), 6),
               "event": event, "state": self._state, "pid": self._owner_pid,
               "session_id": (self._call or {}).get("session_id"),
               "call_id": (self._call or {}).get("call_id"), "detail": detail}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    def _persist(self):
        data = {"pid": self._owner_pid, "state": self._state, "call": self._call,
                "lease": self._lease, "saved_at": round(self.wall(), 3)}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_file)
        except Exception:
            pass

    # ---------- 查询 ----------
    def _remember(self, call_id, result):
        """幂等缓存：只留最近 MAX_CALL_MEMORY 条，避免长跑进程无限增长。"""
        self._calls[call_id] = result
        while len(self._calls) > self.MAX_CALL_MEMORY:
            self._calls.pop(next(iter(self._calls)))

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "call": dict(self._call) if self._call else None,
                    "lease": dict(self._lease) if self._lease else None,
                    "session_topology": self.session_topology,
                    "requires_account_handover": self.requires_account_handover,
                    "send_allowed": self._lease is None or self._lease["owner"] == VIDEO_OWNER}

    # ---------- 来电 ----------
    def on_incoming_call(self, caller, call_id=None, authorized=None):
        """来电入口。幂等：同一个 call_id 重复进来不会产生第二次振铃。"""
        with self._lock:
            caller = str(caller)
            call_id = call_id or ("call-" + uuid.uuid4().hex[:12])
            if call_id in self._calls:
                prev = dict(self._calls[call_id])
                prev["duplicate"] = True
                prev["state"] = self._state        # 报当前真实状态，不回放历史快照
                prev["replayed_decision"] = self._calls[call_id].get("reason")
                self._log("incoming_duplicate", call_id=call_id, caller=caller)
                return prev
            if self._state in (RINGING, AUTHORIZING, IN_CALL):
                res = {"accepted": False, "call_id": call_id, "caller": caller,
                       "state": self._state, "authorized": False,
                       "reason": "busy", "session_id": None}
                self._remember(call_id, res)
                self._log("incoming_rejected", call_id=call_id, caller=caller, reason="busy")
                return dict(res)
            if authorized is None:
                allowed, reason = self.policy.authorize(caller, self.wall())
            else:  # 允许调用方（人工/管理员）直接仲裁
                allowed, reason = bool(authorized), "caller_decision"
            session_id = "video-" + call_id
            self._call = {"call_id": call_id, "caller": caller, "session_id": session_id,
                          "ring_started": self.clock(), "answered_at": None,
                          "last_heartbeat": None, "turn": 0}
            self._state = RINGING if allowed else REJECTED
            res = {"accepted": allowed, "call_id": call_id, "caller": caller,
                   "state": self._state, "authorized": allowed, "reason": reason,
                   "session_id": session_id, "session_topology": self.session_topology}
            self._remember(call_id, res)
            self._persist()
            self._log("incoming_authorize", call_id=call_id, caller=caller,
                      authorized=allowed, reason=reason, session_id=session_id)
            return dict(res)

    def answer(self, call_id=None):
        """接听。幂等；未授权/状态不对一律拒绝，不产生副作用。"""
        with self._lock:
            if self._state == IN_CALL and self._call and \
                    (call_id is None or call_id == self._call["call_id"]):
                res = dict(self._calls.get(self._call["call_id"], {}))
                res.update(answered=True, duplicate=True, state=IN_CALL)
                return res
            if self._state not in (RINGING, AUTHORIZING) or not self._call:
                return {"answered": False, "reason": "not_ringing", "state": self._state}
            if call_id is not None and call_id != self._call["call_id"]:
                return {"answered": False, "reason": "call_id_mismatch", "state": self._state}
            if self.requires_account_handover:
                self._log("answer_blocked_handover", call_id=self._call["call_id"],
                          topology=self.session_topology)
                return {"answered": False, "reason": "account_handover_required",
                        "state": self._state, "session_topology": self.session_topology}
            self._state = IN_CALL
            self._call["answered_at"] = self.clock()
            self._call["last_heartbeat"] = self.clock()
            self._lease = {"owner": VIDEO_OWNER, "call_id": self._call["call_id"],
                           "since": self.clock()}
            self.store.put("video", self._call["session_id"], "world",
                           {"caller": self._call["caller"], "call_id": self._call["call_id"],
                            "opened_at": round(self.wall(), 3)})
            self._persist()
            self._log("answered", call_id=self._call["call_id"])
            return {"answered": True, "state": IN_CALL, "call_id": self._call["call_id"],
                    "session_id": self._call["session_id"],
                    "send_lease": self._lease["owner"]}

    def heartbeat(self, call_id=None):
        with self._lock:
            if self._state != IN_CALL or not self._call:
                return {"ok": False, "reason": "not_in_call", "state": self._state}
            if call_id is not None and call_id != self._call["call_id"]:
                return {"ok": False, "reason": "call_id_mismatch"}
            self._call["last_heartbeat"] = self.clock()
            return {"ok": True, "state": IN_CALL}

    def hangup(self, call_id=None, reason=NORMAL, by="user"):
        """挂断/结束。幂等；任何路径都必须释放发送租约。"""
        with self._lock:
            if self._state not in (RINGING, AUTHORIZING, IN_CALL):
                return {"hung_up": False, "duplicate": True, "reason": "no_active_call",
                        "state": self._state}
            is_admin = str(by) == "admin"
            if self._state == IN_CALL and call_id is not None and \
                    call_id != self._call["call_id"]:
                return {"hung_up": False, "reason": "call_id_mismatch"}
            sid = self._call["session_id"]
            if self._call.get("answered_at") is not None:
                # 通话元数据写 world，**不写 summary**：summary 归 DialogueService 所有
                # （对话侧知道轮次/情绪）。T6 实测过两者都写 summary 会把对话摘要覆盖成 turns:0。
                world = self.store.load("video", sid, "world") or {}
                world = dict(world) if isinstance(world, dict) else {}
                world["call"] = {"call_id": self._call["call_id"],
                                 "caller": self._call["caller"],
                                 "duration": round(self.clock() - self._call["answered_at"], 3),
                                 "hangup_reason": reason,
                                 "turns": self._call.get("turn", 0)}
                self.store.put("video", sid, "world", world)
            self.store.append("video", sid, "archive",
                              {"event": "call_ended", "reason": reason,
                               "by": "admin" if is_admin else by,
                               "at": round(self.wall(), 3)})
            released = self._lease
            self._lease = None
            self._state = ENDED
            self._call["ended_at"] = self.clock()
            self._persist()
            self._log("hangup", reason=reason, by=by, released_lease=released is not None)
            res = {"hung_up": True, "state": ENDED, "session_id": sid,
                   "reason": reason, "lease_released": released is not None}
            self._state = IDLE
            self._call = None
            self._persist()
            return res

    def tick(self):
        """超时看门狗：振铃超时、通话超时、断线（心跳超时）都要释放。"""
        with self._lock:
            if not self._call:
                return {"action": None, "state": self._state}
            now = self.clock()
            if self._state in (RINGING, AUTHORIZING):
                if now - self._call["ring_started"] > self.ring_timeout:
                    self.hangup(reason=RING_TIMEOUT, by="watchdog")
                    return {"action": "hangup", "reason": RING_TIMEOUT}
            elif self._state == IN_CALL:
                if now - self._call["answered_at"] > self.max_call_seconds:
                    self.hangup(reason=CALL_TIMEOUT, by="watchdog")
                    return {"action": "hangup", "reason": CALL_TIMEOUT}
                last = self._call.get("last_heartbeat") or self._call["answered_at"]
                if now - last > self.heartbeat_timeout:
                    self.hangup(reason=DISCONNECTED, by="watchdog")
                    return {"action": "hangup", "reason": DISCONNECTED}
            return {"action": None, "state": self._state}

    # ---------- 发送互斥 ----------
    def send_allowed(self, owner="message"):
        with self._lock:
            if self._lease is None:
                return {"allowed": True, "reason": "idle"}
            if owner == self._lease["owner"]:
                return {"allowed": True, "reason": "lease_holder"}
            return {"allowed": False, "reason": "video_call_in_progress",
                    "holder": self._lease["owner"],
                    "call_id": self._lease["call_id"]}

    def acquire_send_lease(self, owner="message"):
        with self._lock:
            cur = self.send_allowed(owner)
            if cur["allowed"]:
                self._log("send_allowed", owner=owner, reason=cur["reason"])
            else:
                self._log("send_blocked", owner=owner, reason=cur["reason"])
            return cur

    def release_send_lease(self, owner):
        """只允许租约持有者自己释放；外部强释放走 hangup。幂等。"""
        with self._lock:
            if self._lease is None:
                return {"released": False, "reason": "no_lease", "duplicate": True}
            if owner != self._lease["owner"]:
                return {"released": False, "reason": "not_lease_holder",
                        "holder": self._lease["owner"]}
            self._lease = None
            self._log("lease_released", owner=owner)
            self._persist()
            return {"released": True}

    # ---------- 消息与会话（隔离） ----------
    def record_incoming_message(self, chat_id, sender, text, scope="chat", extra=None):
        """收到消息永远记录（通话期间也不例外），只是发送被互斥。"""
        rec = {"sender": str(sender), "text": str(text), "at": round(self.wall(), 3),
               "during_call": self._state == IN_CALL,
               "call_id": (self._call or {}).get("call_id")}
        if extra:
            rec.update(extra)
        self.store.append(scope, chat_id, "history", rec)
        self._log("message_recorded", chat_id=chat_id, sender=str(sender),
                  during_call=rec["during_call"])
        return rec

    def video_turn(self, text, reply, emotion=None):
        """视频会话轮次：只写视频命名空间，绝不落到普通聊天。"""
        with self._lock:
            if not self._call or self._state != IN_CALL:
                return {"ok": False, "reason": "not_in_call"}
            sid = self._call["session_id"]
            self._call["turn"] = self._call.get("turn", 0) + 1
            rec = {"turn": self._call["turn"], "user": str(text), "ai": str(reply),
                   "emotion": emotion, "at": round(self.wall(), 3)}
            self.store.append("video", sid, "history", rec)
            self._log("video_turn", turn=rec["turn"], emotion=emotion)
            return {"ok": True, "session_id": sid, "turn": rec["turn"]}

    # ---------- 崩溃恢复 ----------
    def recover_from_crash(self):
        """进程重启后从落盘状态重建：残留 IN_CALL 一律判崩溃并释放租约。幂等。"""
        with self._lock:
            if not self._state_file.exists():
                self._recovered = True
                return {"recovered": False, "reason": "no_state_file"}
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception as exc:
                return {"recovered": False, "reason": "unreadable: %s" % exc}
            stale = data.get("pid") != self._owner_pid and data.get("state") in (
                RINGING, AUTHORIZING, IN_CALL)
            if not stale:
                self._recovered = True
                return {"recovered": False, "reason": "clean_state",
                        "previous_state": data.get("state")}
            prev_call = data.get("call") or {}
            sid = prev_call.get("session_id")
            if sid:
                self.store.append("video", sid, "archive",
                                  {"event": "crash_recovery", "reason": CRASHED,
                                   "at": round(self.wall(), 3)})
            self._lease = None
            self._call = None
            self._state = IDLE
            self._persist()
            self._log("crash_recovered", previous_state=data.get("state"),
                      previous_pid=data.get("pid"), session_id=sid)
            return {"recovered": True, "previous_state": data.get("state"),
                    "previous_pid": data.get("pid"), "session_id": sid,
                    "lease_released": bool(data.get("lease"))}

    # ---------- 管理员 ----------
    def set_auto_answer(self, enabled, by="admin"):
        self.policy.auto_answer = bool(enabled)
        self._log("auto_answer_set", enabled=bool(enabled), by=by)
        return {"auto_answer": self.policy.auto_answer}

    def force_hangup(self, reason=FORCED):
        return self.hangup(reason=reason, by="admin")

    def debug_send_lease_check(self):
        """验收用：本实例所在 store 的 IDLE/IN_CALL 发送判定（不改本实例状态）。"""
        return debug_send_lease_check(self.store.root)


def debug_send_lease_check(store_root=None):
    """验收辅助：IDLE 允许发送；模拟 IN_CALL 必须拒绝且原因明确。"""
    import tempfile
    parent = Path(store_root or (ROOT / "logs"))
    parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="coord-", dir=str(parent))
    c = SessionCoordinator(store_root=tmp, log_path=Path(tmp) / "coordinator.jsonl",
                           session_topology="same_process_injected")
    idle = c.send_allowed("message")
    with c._lock:
        c._state = IN_CALL
        c._call = {"call_id": "sim-1", "caller": "10001",
                   "session_id": "video-sim-1", "turn": 0}
        c._lease = {"owner": VIDEO_OWNER, "call_id": "sim-1", "since": 0.0}
    busy = c.send_allowed("message")
    video = c.send_allowed(VIDEO_OWNER)
    return {"idle": idle, "in_call_message": busy, "in_call_video": video}


if __name__ == "__main__":
    print(json.dumps(debug_send_lease_check(), ensure_ascii=False, indent=2))
