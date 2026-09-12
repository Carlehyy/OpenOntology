"""Pure Action definition contracts, organized by rule responsibility."""
from __future__ import annotations

from typing import Any

from app.ontologies.formal_modeling.action_validation_primitives import (
    _MISSING,
    _SUPPORTED_RULE_TYPES,
    _TPL_RE,
    _definition_field,
    _definition_id,
    _definition_properties,
    _parameter_default,
    _parameter_type_error,
    _static_sample,
    _validate_expression_property_references,
)
from app.ontologies.formal_modeling.safe_eval import (
    SafeEvalError,
    validate_safe_expression,
)
from app.ontologies.formal_modeling.webhook_dispatcher import (
    WebhookDispatchError,
    preview_webhook,
)


def _snapshot_rule_safe(rules: list[dict]) -> bool:
    for rule in rules:
        rule_type = rule.get("type")
        if rule_type not in ("validation", "notification", "webhook"):
            return False
        if (
            rule_type == "notification"
            and (rule.get("config") or {}).get("recipientSource") == "link"
        ):
            # A deleted leave target no longer has a live source row from which
            # link-scoped recipient resolution can be performed.
            return False
    return True


def action_supports_snapshot_execution(action) -> bool:
    """Whether an Action can execute after its live target row was deleted.

    Sentinel ``on_enter_leave`` stores an immutable target snapshot for leave
    replay.  Keep this decision shared with the execution engine so release
    gates cannot approve a rule set the runtime will inevitably reject.
    """
    raw_rules = _definition_field(action, "rules", default=[]) or []
    if (
        not isinstance(raw_rules, list)
        or any(not isinstance(rule, dict) for rule in raw_rules)
    ):
        return False
    enabled_rules = [
        rule for rule in raw_rules
        if isinstance(rule, dict) and rule.get("enabled", True)
    ]
    return _snapshot_rule_safe(enabled_rules)


class _ActionDefinitionValidator:
    """Ordered, side-effect-free validator for one Action definition.

    ``require_executable_action_rules=False`` 是草稿保存期的分层口径：
    「无启用的可执行副作用规则」从 error 降级为 warning（由 ``warnings`` 收集、
    经外层 ``warnings_out`` 透出）；其余校验（参数/结构/引用）不受影响，
    试跑与发布保持默认严格。
    """

    def __init__(
        self,
        action,
        object_types: list,
        link_types: list,
        functions: list | None,
        *,
        require_executable_action_rules: bool = True,
    ) -> None:
        self.action = action
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.require_executable_action_rules = require_executable_action_rules
        self.requires_approval = bool(
            _definition_field(
                action,
                "requiresApproval",
                "requires_approval",
                default=False,
            )
        )
        self.action_name = str(
            _definition_field(
                action,
                "displayName",
                "display_name",
                "name",
                default=_definition_id(action),
            )
            or _definition_id(action)
        )
        self.object_types_by_id = {
            _definition_id(item): item
            for item in object_types
            if _definition_id(item)
        }
        self.link_types_by_id = {
            _definition_id(item): item
            for item in link_types
            if _definition_id(item)
        }
        self.functions_by_id = (
            {
                _definition_id(item): item
                for item in functions
                if _definition_id(item)
            }
            if functions is not None
            else None
        )
        self.bound_type_id = str(
            _definition_field(
                action,
                "objectTypeId",
                "object_type_id",
                default="",
            )
            or ""
        )
        self.bound_properties: dict[str, dict] = {}
        self.bound_samples: dict[str, Any] = {}
        self.parameter_defs: dict[str, dict] = {}
        self.parameter_samples: dict[str, Any] = {}
        self.created_types_seen: set[str] = set()

    def validate(self) -> list[str]:
        self._prepare_bound_type()
        self._validate_parameters()
        self._validate_bound_action_function()

        raw_rules = _definition_field(self.action, "rules", default=[]) or []
        if not isinstance(raw_rules, list):
            return [
                *self.errors,
                f"动作「{self.action_name}」rules 必须是数组",
            ]

        ordered = sorted(
            self._normalize_rules(raw_rules),
            key=lambda item: item.get("order", 0),
        )
        self._validate_rule_order(ordered)
        for rule in ordered:
            self._validate_rule(rule)
        return self.errors

    def _prepare_bound_type(self) -> None:
        bound_type = self.object_types_by_id.get(self.bound_type_id)
        if self.bound_type_id and bound_type is None:
            self.errors.append(
                f"动作「{self.action_name}」绑定的对象类型不存在: "
                f"{self.bound_type_id}"
            )
        self.bound_properties = (
            _definition_properties(bound_type)
            if bound_type is not None
            else {}
        )
        self.bound_samples = {
            name: _static_sample(definition)
            for name, definition in self.bound_properties.items()
        }

    def _validate_parameters(self) -> None:
        raw_parameters = (
            _definition_field(self.action, "parameters", default=[]) or []
        )
        if not isinstance(raw_parameters, list):
            self.errors.append(
                f"动作「{self.action_name}」parameters 必须是数组"
            )
            raw_parameters = []
        for parameter in raw_parameters:
            if not isinstance(parameter, dict):
                self.errors.append(
                    f"动作「{self.action_name}」参数定义必须是对象"
                )
                continue
            name = str(parameter.get("name") or "").strip()
            if not name:
                self.errors.append(
                    f"动作「{self.action_name}」存在缺少 name 的参数"
                )
                continue
            if name in self.parameter_defs:
                self.errors.append(
                    f"动作「{self.action_name}」参数重复: {name}"
                )
                continue
            self.parameter_defs[name] = parameter
            default = _parameter_default(parameter)
            if default is not _MISSING:
                type_error = _parameter_type_error(
                    name,
                    str(parameter.get("type") or "string"),
                    default,
                )
                if type_error:
                    self.errors.append(
                        f"动作「{self.action_name}」{type_error}"
                    )
        self.parameter_samples = {
            name: _static_sample(definition)
            for name, definition in self.parameter_defs.items()
        }

    def _validate_bound_action_function(self) -> None:
        function_id = str(
            _definition_field(
                self.action,
                "validationFunctionId",
                "validation_function_id",
                default="",
            )
            or ""
        ).strip()
        if not function_id or self.functions_by_id is None:
            return
        function = self.functions_by_id.get(function_id)
        if function is None:
            self.errors.append(
                f"动作「{self.action_name}」绑定的校验函数不存在: "
                f"{function_id}"
            )
            return
        function_type = str(
            _definition_field(
                function,
                "functionType",
                "function_type",
                default="",
            )
            or ""
        )
        if function_type != "action_validation":
            self.errors.append(
                f"动作「{self.action_name}」绑定的函数不是 "
                f"action_validation: {function_id}"
            )

    def _normalize_rules(self, raw_rules: list) -> list[dict]:
        rules: list[dict] = []
        rule_ids: set[str] = set()
        for index, rule in enumerate(raw_rules):
            if not isinstance(rule, dict):
                self.errors.append(
                    f"动作「{self.action_name}」第 {index + 1} 条规则必须是对象"
                )
                continue
            if not rule.get("enabled", True):
                continue
            rule_type = rule.get("type")
            label = str(rule.get("name") or rule_type or index + 1)
            if rule_type not in _SUPPORTED_RULE_TYPES:
                self.errors.append(
                    f"动作「{self.action_name}」规则「{label}」类型不支持: "
                    f"{rule_type}"
                )
                continue
            config = rule.get("config", {})
            if not isinstance(config, dict):
                self.errors.append(
                    f"动作「{self.action_name}」规则「{label}」config 必须是对象"
                )
                continue
            order = rule.get("order", 0)
            if not isinstance(order, (int, float)) or isinstance(order, bool):
                self.errors.append(
                    f"动作「{self.action_name}」规则「{label}」order 必须是数字"
                )
                continue
            rule_id = str(rule.get("id") or "").strip()
            if rule_id:
                if rule_id in rule_ids:
                    self.errors.append(
                        f"动作「{self.action_name}」规则 id 重复: {rule_id}"
                    )
                    continue
                rule_ids.add(rule_id)
            rules.append(rule)
        return rules

    def _validate_rule_order(self, ordered: list[dict]) -> None:
        effect_rules = [
            rule for rule in ordered if rule.get("type") != "validation"
        ]
        if not effect_rules:
            message = f"动作「{self.action_name}」没有启用的可执行副作用规则"
            if self.requires_approval:
                # 对齐运行时 approval_proposal_only 路径（action_engine）：
                # requiresApproval 动作无副作用规则是语义合法 —— 执行挂起
                # 审批提案，不记伪成功；两种模式都不报错。
                # 注：该对齐对真实执行成立；dry_run 下 approval_proposal_only
                # 为 False，运行时仍会拒绝 —— 试跑只做结构校验不执行动作，
                # 静态豁免因此不会放行出"试跑期才炸"的动作。
                pass
            elif self.require_executable_action_rules:
                self.errors.append(message)
            else:
                # 草稿保存期降级：不阻断保存，作为 warnings 透出待人工形式化。
                self.warnings.append(message)
        first_webhook = next(
            (
                index
                for index, rule in enumerate(ordered)
                if rule.get("type") == "webhook"
            ),
            None,
        )
        if first_webhook is not None and any(
            rule.get("type") not in ("validation", "webhook")
            for rule in ordered[first_webhook + 1 :]
        ):
            self.errors.append(
                f"动作「{self.action_name}」Webhook 必须位于所有本地副作用规则之后"
            )
        first_effect = next(
            (
                index
                for index, rule in enumerate(ordered)
                if rule.get("type") != "validation"
            ),
            None,
        )
        if first_effect is not None and any(
            rule.get("type") == "validation"
            for rule in ordered[first_effect + 1 :]
        ):
            self.errors.append(
                f"动作「{self.action_name}」validation 必须位于所有副作用规则之前"
            )

    def _rule_error(self, label: str, message: str) -> None:
        self.errors.append(
            f"动作「{self.action_name}」规则「{label}」{message}"
        )

    def _validate_function_reference(
        self,
        label: str,
        function_id: Any,
        purpose: str,
        *,
        expected_type: str | None = None,
    ) -> None:
        fid = str(function_id or "").strip()
        if not fid:
            self._rule_error(label, f"{purpose}缺少 functionId")
        elif self.functions_by_id is not None:
            function = self.functions_by_id.get(fid)
            if function is None:
                self._rule_error(label, f"{purpose}引用的函数不存在: {fid}")
            elif expected_type:
                function_type = str(
                    _definition_field(
                        function,
                        "functionType",
                        "function_type",
                        default="",
                    )
                    or ""
                )
                if function_type != expected_type:
                    self._rule_error(
                        label,
                        f"{purpose}引用的函数类型必须是 {expected_type}: "
                        f"{fid}",
                    )

    def _validate_mapping_source(
        self,
        label: str,
        mapping: dict,
        *,
        source_key: str = "sourceType",
        value_key: str = "sourceValue",
    ) -> None:
        source_type = str(mapping.get(source_key) or "").strip()
        source_value = str(mapping.get(value_key) or "").strip()
        if source_type == "parameter":
            if source_value not in self.parameter_defs:
                self._rule_error(
                    label,
                    f"引用的动作参数不存在: {source_value}",
                )
        elif source_type in ("source_property", "property"):
            if not self.bound_type_id:
                self._rule_error(label, "属性来源需要动作绑定对象类型")
            elif (
                self.bound_properties
                and source_value not in self.bound_properties
            ):
                self._rule_error(
                    label,
                    f"引用的源对象属性不存在: {source_value}",
                )
        elif source_type == "constant":
            return
        elif source_type == "expression":
            if mapping.get("functionId"):
                self._validate_function_reference(
                    label,
                    mapping.get("functionId"),
                    "表达式",
                )
            elif not source_value:
                self._rule_error(label, "expression 缺少表达式")
            else:
                try:
                    validate_safe_expression(
                        source_value,
                        allowed_names={"params", "object"},
                    )
                    _validate_expression_property_references(
                        source_value,
                        {
                            "params": self.parameter_samples,
                            "object": self.bound_samples,
                        },
                    )
                except SafeEvalError as exc:
                    self._rule_error(label, f"表达式无效: {exc}")
        elif source_type == "function":
            self._validate_function_reference(
                label,
                mapping.get("functionId"),
                "函数来源",
            )
        else:
            self._rule_error(
                label,
                f"不支持的取值来源: {source_type or '(空)'}",
            )

    def _validate_rule(self, rule: dict) -> None:
        rule_type = str(rule.get("type") or "")
        label = str(rule.get("name") or rule_type)
        config = rule.get("config") or {}
        if rule_type == "validation":
            self._validate_validation_rule(label, config)
        elif rule_type == "create_object":
            created_type_id = self._validate_create_object_rule(label, config)
            if created_type_id is not None:
                self.created_types_seen.add(created_type_id)
        elif rule_type == "update_property":
            self._validate_update_property_rule(label, config)
        elif rule_type in ("create_link", "delete_link"):
            self._validate_link_rule(rule_type, label, config)
        elif rule_type == "notification":
            self._validate_notification_rule(label, config)
        elif rule_type == "webhook":
            self._validate_webhook_rule(label, config)

    def _validate_validation_rule(self, label: str, config: dict) -> None:
        function_id = config.get("functionId")
        condition = str(config.get("condition") or "").strip()
        if function_id:
            self._validate_function_reference(
                label,
                function_id,
                "校验规则",
                expected_type="action_validation",
            )
        elif condition:
            try:
                validate_safe_expression(
                    condition,
                    allowed_names={"params", "object"},
                )
                _validate_expression_property_references(
                    condition,
                    {
                        "params": self.parameter_samples,
                        "object": self.bound_samples,
                    },
                )
            except SafeEvalError as exc:
                self._rule_error(label, f"校验表达式无效: {exc}")
        else:
            self._rule_error(label, "未配置 functionId 或 condition")

    def _validate_create_object_rule(
        self,
        label: str,
        config: dict,
    ) -> str | None:
        target_type_id = str(config.get("targetObjectTypeId") or "")
        target_type = self.object_types_by_id.get(target_type_id)
        if target_type is None:
            self._rule_error(
                label,
                f"目标对象类型不存在: {target_type_id or '(空)'}",
            )
            return None
        mappings = config.get("propertyMappings", [])
        if not isinstance(mappings, list):
            self._rule_error(label, "propertyMappings 必须是数组")
            return None
        target_properties = _definition_properties(target_type)
        mapped: set[str] = set()
        for mapping in mappings:
            if not isinstance(mapping, dict):
                self._rule_error(label, "属性映射必须是对象")
                continue
            target_property = str(mapping.get("targetProperty") or "")
            if not target_property:
                self._rule_error(label, "属性映射缺少 targetProperty")
                continue
            if target_property in mapped:
                self._rule_error(
                    label,
                    f"目标属性重复映射: {target_property}",
                )
            mapped.add(target_property)
            if (
                target_properties
                and target_property not in target_properties
            ):
                self._rule_error(
                    label,
                    f"目标对象属性不存在: {target_property}",
                )
            elif target_property in target_properties:
                definition = target_properties[target_property]
                if (
                    definition.get("source") == "computed"
                    or bool(definition.get("computed"))
                ):
                    self._rule_error(
                        label,
                        "派生属性不能由 create_object 映射写入: "
                        f"{target_property}",
                    )
            self._validate_mapping_source(label, mapping)
        primary_key = str(
            _definition_field(
                target_type,
                "primaryKey",
                "primary_key",
                default="",
            )
            or ""
        )
        for name, definition in target_properties.items():
            computed = (
                definition.get("source") == "computed"
                or bool(definition.get("computed"))
            )
            is_primary = bool(
                primary_key
                and primary_key
                in (str(definition.get("id") or ""), name)
            )
            if (
                definition.get("required")
                and not computed
                and not is_primary
                and name not in mapped
            ):
                self._rule_error(
                    label,
                    f"未映射必填目标属性: {name}",
                )
        return target_type_id

    def _validate_update_property_rule(
        self,
        label: str,
        config: dict,
    ) -> None:
        target_property = str(config.get("targetProperty") or "")
        if not self.bound_type_id:
            self._rule_error(label, "update_property 需要动作绑定对象类型")
        elif (
            self.bound_properties
            and target_property not in self.bound_properties
        ):
            self._rule_error(
                label,
                f"目标对象属性不存在: {target_property or '(空)'}",
            )
        elif target_property in self.bound_properties:
            definition = self.bound_properties[target_property]
            if (
                definition.get("source") == "computed"
                or bool(definition.get("computed"))
            ):
                self._rule_error(
                    label,
                    "update_property 不能写入派生属性: "
                    f"{target_property}",
                )
        self._validate_mapping_source(
            label,
            {
                "sourceType": config.get("valueSource", "constant"),
                "sourceValue": config.get("value", ""),
                "functionId": config.get("functionId"),
            },
        )

    def _validate_link_rule(
        self,
        rule_type: str,
        label: str,
        config: dict,
    ) -> None:
        link_type_id = str(config.get("linkTypeId") or "")
        link_type = self.link_types_by_id.get(link_type_id)
        if link_type is None:
            self._rule_error(
                label,
                f"链接类型不存在: {link_type_id or '(空)'}",
            )
            return
        source_type_id = str(
            _definition_field(
                link_type,
                "sourceObjectTypeId",
                "source_object_type_id",
                default="",
            )
            or ""
        )
        target_type_id = str(
            _definition_field(
                link_type,
                "targetObjectTypeId",
                "target_object_type_id",
                default="",
            )
            or ""
        )
        if self.bound_type_id != source_type_id:
            self._rule_error(
                label,
                "动作对象类型必须是链接源类型: "
                f"{self.bound_type_id or '(空)'} != {source_type_id}",
            )
        if rule_type == "create_link":
            self._validate_create_link_target(
                label,
                config,
                target_type_id,
            )
        else:
            self._validate_delete_link_condition(
                label,
                config,
                link_type,
                target_type_id,
            )

    def _validate_create_link_target(
        self,
        label: str,
        config: dict,
        target_type_id: str,
    ) -> None:
        target_source = str(config.get("targetSource") or "parameter")
        target_value = str(config.get("targetValue") or "")
        if target_source == "parameter":
            if target_value not in self.parameter_defs:
                self._rule_error(
                    label,
                    f"链接目标参数不存在: {target_value}",
                )
        elif target_source == "created_object":
            if target_value not in self.created_types_seen:
                self._rule_error(
                    label,
                    "created_object 必须引用前序创建的对象类型: "
                    f"{target_value}",
                )
            if target_value != target_type_id:
                self._rule_error(
                    label,
                    "created_object 类型与链接目标类型不一致: "
                    f"{target_value} != {target_type_id}",
                )
        elif target_source == "expression":
            if not target_value:
                self._rule_error(label, "链接目标表达式为空")
            else:
                try:
                    validate_safe_expression(
                        target_value,
                        allowed_names={"params", "object"},
                    )
                except SafeEvalError as exc:
                    self._rule_error(
                        label,
                        f"链接目标表达式无效: {exc}",
                    )
        elif target_source == "source":
            if self.bound_type_id != target_type_id:
                self._rule_error(
                    label,
                    "source 仅适用于源类型与目标类型相同的链接",
                )
        else:
            self._rule_error(
                label,
                f"不支持的链接目标来源: {target_source}",
            )

    def _validate_delete_link_condition(
        self,
        label: str,
        config: dict,
        link_type,
        target_type_id: str,
    ) -> None:
        condition = str(config.get("condition") or "").strip()
        if not condition:
            return
        target_type = self.object_types_by_id.get(target_type_id)
        target_properties = (
            _definition_properties(target_type)
            if target_type is not None
            else {}
        )
        link_properties = _definition_properties(link_type)
        target_samples = {
            name: _static_sample(definition)
            for name, definition in target_properties.items()
        }
        link_samples = {
            name: _static_sample(definition)
            for name, definition in link_properties.items()
        }
        try:
            validate_safe_expression(
                condition,
                allowed_names={
                    "object",
                    "source",
                    "target",
                    "link",
                    "params",
                },
            )
            _validate_expression_property_references(
                condition,
                {
                    "object": self.bound_samples,
                    "source": self.bound_samples,
                    "target": target_samples,
                    "link": link_samples,
                    "params": self.parameter_samples,
                },
            )
        except SafeEvalError as exc:
            self._rule_error(label, f"删除条件无效: {exc}")

    def _validate_notification_rule(
        self,
        label: str,
        config: dict,
    ) -> None:
        channel = str(config.get("channel") or "internal")
        if channel not in ("internal", "in_app", "in-app"):
            self._rule_error(
                label,
                f"通知通道 {channel} 未配置可靠投递器",
            )
        recipient_source = str(
            config.get("recipientSource") or "constant"
        )
        recipient = str(config.get("recipient") or "")
        if recipient_source == "constant":
            if not recipient.strip():
                self._rule_error(label, "固定收件人不能为空")
        elif recipient_source == "parameter":
            if recipient not in self.parameter_defs:
                self._rule_error(
                    label,
                    f"收件人参数不存在: {recipient}",
                )
        elif recipient_source == "property":
            if not recipient or recipient not in self.bound_properties:
                self._rule_error(
                    label,
                    f"收件人对象属性不存在: {recipient}",
                )
        elif recipient_source == "link":
            self._validate_link_recipient(label, config)
        else:
            self._rule_error(
                label,
                f"不支持的收件人来源: {recipient_source}",
            )
        self._validate_message_template(label, config)

    def _validate_link_recipient(self, label: str, config: dict) -> None:
        link_type_id = str(config.get("linkTypeId") or "")
        link_type = self.link_types_by_id.get(link_type_id)
        if link_type is None:
            self._rule_error(
                label,
                f"收件人链接类型不存在: {link_type_id or '(空)'}",
            )
            return
        source_type_id = str(
            _definition_field(
                link_type,
                "sourceObjectTypeId",
                "source_object_type_id",
                default="",
            )
            or ""
        )
        target_type_id = str(
            _definition_field(
                link_type,
                "targetObjectTypeId",
                "target_object_type_id",
                default="",
            )
            or ""
        )
        if source_type_id != self.bound_type_id:
            self._rule_error(
                label,
                "收件人链接源类型与动作对象类型不一致",
            )
        target_type = self.object_types_by_id.get(target_type_id)
        related_properties = (
            _definition_properties(target_type)
            if target_type is not None
            else {}
        )
        recipient_property = str(
            config.get("recipientProperty") or "email"
        )
        if (
            related_properties
            and recipient_property not in related_properties
        ):
            self._rule_error(
                label,
                f"关联收件人属性不存在: {recipient_property}",
            )

    def _validate_message_template(self, label: str, config: dict) -> None:
        message_template = str(config.get("messageTemplate") or "")
        if not message_template.strip():
            self._rule_error(label, "通知消息模板不能为空")
        for namespace, key in _TPL_RE.findall(message_template):
            if namespace in ("param", "params"):
                if key not in self.parameter_defs:
                    self._rule_error(
                        label,
                        f"通知模板参数不存在: {key}",
                    )
            elif key not in self.bound_properties:
                self._rule_error(
                    label,
                    f"通知模板对象属性不存在: {key}",
                )
        residue = _TPL_RE.sub("", message_template)
        if "{{" in residue or "}}" in residue:
            self._rule_error(label, "通知模板包含无法解析的占位符")

    def _validate_webhook_rule(self, label: str, config: dict) -> None:
        try:
            preview_webhook(
                config,
                params=self.parameter_samples,
                object_props=self.bound_samples,
            )
        except WebhookDispatchError as exc:
            self._rule_error(label, f"Webhook 配置无效: {exc}")


def validate_action_definition(
    action,
    object_types: list,
    link_types: list,
    functions: list | None = None,
    *,
    require_executable_action_rules: bool = True,
    warnings_out: list[str] | None = None,
) -> list[str]:
    """Validate one Action without database writes or outbound requests."""
    validator = _ActionDefinitionValidator(
        action, object_types, link_types, functions,
        require_executable_action_rules=require_executable_action_rules,
    )
    errors = validator.validate()
    if warnings_out is not None:
        warnings_out.extend(validator.warnings)
    return errors
