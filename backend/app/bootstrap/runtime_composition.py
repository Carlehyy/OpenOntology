"""Shared runtime port composition for every process executing assistant work.

Both the HTTP application (``app.main``) and the NATS executor bootstrap in
``app.data_channel.pipeline_tasks.nats_executor`` must install the same leaf
adapters.  Keeping the registrations here prevents the worker from starting
with an empty query provider registry (or unset remote/action ports) merely
because only ``main.py`` had wired them.

This is process bootstrap, not a new business layer: each adapter still belongs
to its canonical package and the ontology read port still receives its query
DTO factories from outside the ontology domain.
"""
from __future__ import annotations

from functools import partial


def install_runtime_ports() -> None:
    """Install the cross-domain leaf ports used by agent execution.

    Idempotent: registration helpers overwrite the same kind/singleton, so a
    repeated call (tests, repeated bootstrap) changes no semantics.
    """
    from app.data_channel.action_write_port import AssetWritePortAdapter
    from app.data_channel.integrated.binding_port import (
        IntegratedRemoteBindingValidator,
    )
    from app.data_channel.pipeline_tasks.dispatch import (
        dispatch_ontology_assistant_run,
    )
    from app.data_channel.query.contracts import (
        ColumnSpec,
        Relation,
        register_provider,
    )
    from app.data_channel.query.ontology_adapter import OntologyQueryAdapter
    from app.ontologies.agent_confirmation.write_port import (
        set_action_write_port,
    )
    from app.ontologies.agent_query.read_port import resolve_ontology_relation
    from app.ontologies.agent_query.remote_query import set_remote_query_port
    from app.ontologies.agent_runtime import run_service
    from app.ontologies.mappings.remote_binding import (
        set_remote_binding_validator,
    )

    register_provider(
        "ontology",
        partial(
            resolve_ontology_relation,
            column_spec_factory=ColumnSpec,
            relation_factory=Relation,
        ),
    )
    set_remote_query_port(OntologyQueryAdapter())
    set_action_write_port(AssetWritePortAdapter())
    set_remote_binding_validator(IntegratedRemoteBindingValidator())
    # run 账本不反向 import 数据通道：组成根把统一派发端口注入 run_service。
    run_service.set_dispatch_run_port(dispatch_ontology_assistant_run)
