/** 发布弹窗按本体回填：纯函数，便于单测（多本体发布语义）。 */
import type { WorldModelServiceInfo } from '@/api/worldModel'

/**
 * 按本体找该项目已发布的服务：同一本体重发布 = 覆盖该服务（回填其注册信息），
 * 换一个本体 = 新增服务（回填项目默认值）。找不到返回 null。
 */
export function findServiceByOntology(
  services: WorldModelServiceInfo[],
  ontologyId: string,
): WorldModelServiceInfo | null {
  if (!ontologyId) return null
  return services.find(
    service => service.applicable_object_types?.ontology_id === ontologyId,
  ) ?? null
}
