"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["spare_reservations"] = self.repository.reservations_for_record(record_id)
        return record

    def _reservation_op(self, action: str, record: Dict[str, Any], data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if action == "mobilize":
            stock = self.repository.get_stock(int(data["stock_id"]))
            payload = record["payload"]
            if stock["cable"] != payload.get("cable") or stock["segment"] != payload.get("segment"):
                raise ValidationError("所选仓库备缆与故障光缆区段不匹配")
            return {"kind": "reserve", "stock_id": int(stock["id"]), "reserved_km": float(payload["required_spare_km"])}
        if action == "splice":
            return {"kind": "consume", "used_km": float(data["spare_used_km"])}
        if action == "cancel":
            return {"kind": "release"}
        return None

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        reservation_op = self._reservation_op(action, record, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            reservation_op=reservation_op,
        )

    def register_stock(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_stock(actor.role):
            raise PermissionDenied("角色无权登记备缆台账")
        prepared = self.rules.validate_stock(payload or {})
        return self.repository.create_stock(prepared["warehouse"], prepared["cable"], prepared["segment"], float(prepared["total_km"]), actor.user_id)

    def adjust_stock(self, actor: Actor, stock_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_stock(actor.role):
            raise PermissionDenied("角色无权调整备缆台账")
        delta = self.rules.validate_stock_adjust(payload or {})
        return self.repository.adjust_stock(int(stock_id), delta, actor.user_id)

    def list_stock(self, actor: Actor, cable: Optional[str] = None, segment: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_stock(cable=cable, segment=segment)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
