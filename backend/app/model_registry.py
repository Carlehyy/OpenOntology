"""Import every SQLAlchemy model into the shared Base metadata.

Alembic, tests and application startup must see the same table registry.  A
partial registry made fresh-database migrations depend on an earlier app
``create_all`` side effect and caused upgrades to fail on missing formal and
sentinel tables.
"""
from __future__ import annotations

from importlib import import_module


MODEL_MODULES = (
    "app.auth.models",
    "app.data_channel.connections.models",
    "app.data_channel.curated.models",
    "app.data_channel.datasets.models",
    "app.data_channel.datasets.sharing_models",
    "app.data_channel.file_assets.models",
    "app.data_channel.pipeline_tasks.models",
    "app.data_channel.pipelines.models",
    "app.data_channel.steward.models",
    "app.data_channel.sync_tasks.models",
    "app.events.models",
    "app.exploration.models",
    "app.inbox.models",
    "app.model_configs.models",
    "app.ontologies.actions.models",
    "app.ontologies.actions.v2_models",
    "app.ontologies.agent_runtime.models",
    "app.ontologies.decision_simulation.models",
    "app.ontologies.attribute_schemas.models",
    "app.ontologies.entities.models",
    "app.ontologies.formal_modeling.models",
    "app.ontologies.inference.models",
    "app.ontologies.logic.models",
    "app.ontologies.logic.v2_models",
    "app.ontologies.mappings.models",
    "app.ontologies.projects.models",
    "app.ontologies.relations.models",
    "app.ontologies.sentinels.models",
    "app.ontologies.versions.models",
    "app.platform.observability.models",
    "app.settings.domains.models",
    "app.settings.object_storage.models",
    "app.settings.workflows.models",
    "app.super_assistant.models",
    "app.super_assistant.kernel.models",
    "app.tickets.models",
    "app.world_model.models",
)


def import_all_models() -> None:
    for module in MODEL_MODULES:
        import_module(module)
