import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import { KeyIcon, BoltIcon } from '@heroicons/react/24/outline';
import type { ObjectType } from '../../types/ontology';
import { useOntologyStore } from '../../store/ontologyStore';
import { objectTypeIconGlyph } from '../../utils/objectTypeIcon';

interface ObjectTypeNodeProps {
  data: ObjectType;
  selected?: boolean;
}

/**
 * 四侧锚点：边在渲染期按两节点相对位置选侧（见 utils/routeEdgeHandles），
 * 拖拽节点时连线端点自动换到更合适的侧面。
 * 仅「左进 / 右出」保留可见圆点，维持拖线创建关系的既有交互；
 * 其余锚点隐形且不可作为连线起点，只供边端点吸附。
 */
const HANDLE_SIDES = [
  ['top', Position.Top],
  ['right', Position.Right],
  ['bottom', Position.Bottom],
  ['left', Position.Left],
] as const;

const VISIBLE_TARGET_HANDLE = '!w-3 !h-3 !bg-surface-400 !border-2 !border-surface-700';
const VISIBLE_SOURCE_HANDLE = '!w-3 !h-3 !bg-indigo-500 !border-2 !border-surface-700';
const INVISIBLE_HANDLE = '!h-2 !w-2 !border-0 !bg-transparent !opacity-0 !pointer-events-none';

const ObjectTypeNode = memo(({ data, selected }: ObjectTypeNodeProps) => {
  const setSelectedNode = useOntologyStore((state) => state.setSelectedNode);
  const openPanel = useOntologyStore((state) => state.openPanel);
  const ontology = useOntologyStore((state) => state.ontology);
  const readOnly = Boolean((data as ObjectType & { __readOnly?: boolean }).__readOnly);
  
  // 获取关联到这个对象类型的动作
  const relatedActions = ontology?.actions.filter(a => a.objectTypeId === data.id) || [];

  const handleDoubleClick = () => {
    setSelectedNode(data.id);
    openPanel('edit', 'objectType');
  };

  const visibleProperties = data.properties.slice(0, 5);
  const remainingCount = data.properties.length - 5;
  const propertyLabel = (prop: ObjectType['properties'][number]) => prop.displayName || prop.name;
  const iconGlyph = objectTypeIconGlyph(data.icon);

  return (
    <div
      onDoubleClick={handleDoubleClick}
      title={readOnly ? '双击查看对象定义；拖动可保存画布布局' : '双击编辑对象定义'}
      className={`
        min-w-[240px] max-w-[320px] rounded-xl overflow-hidden
        bg-gradient-to-b from-surface-800 to-surface-900
        border-2 transition-all duration-200
        ${selected ? 'border-indigo-500 node-selected' : 'border-surface-600 hover:border-surface-500'}
      `}
      style={{
        boxShadow: selected
          ? '0 0 30px var(--color-node-shadow-selected)'
          : '0 4px 20px var(--color-node-shadow)',
      }}
    >
      {/* Header */}
      <div 
        className="px-4 py-3 flex items-center gap-3"
        style={{ 
          background: `linear-gradient(135deg, ${data.color || '#6366f1'}22 0%, transparent 100%)`,
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <div 
          className="w-8 h-8 rounded-lg flex items-center justify-center"
          style={{ backgroundColor: `${data.color || '#6366f1'}33` }}
        >
          <span className="text-lg" data-testid="object-type-icon">{iconGlyph}</span>
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-display font-semibold text-surface-100 truncate">
            {data.displayName}
          </h3>
          <p className="text-xs text-surface-500 font-mono truncate">{data.name}</p>
        </div>
      </div>

      {/* Properties */}
      <div className="px-4 py-3 space-y-1.5">
        {visibleProperties.map((prop) => (
          <div 
            key={prop.id} 
            className="flex items-center justify-between gap-2 text-sm"
          >
            <div className="flex items-center gap-2 min-w-0 flex-1">
              {(prop.id === data.primaryKey || prop.name === data.primaryKey) && (
                <KeyIcon className="w-3.5 h-3.5 text-yellow-500 flex-shrink-0" />
              )}
              <span className={`truncate ${prop.required ? 'text-surface-200' : 'text-surface-400'}`}>
                {propertyLabel(prop)}
              </span>
            </div>
            <span className={`type-badge type-${prop.type} flex-shrink-0`}>
              {prop.type}
            </span>
          </div>
        ))}
        {remainingCount > 0 && (
          <p className="text-xs text-surface-500 pt-1">
            + {remainingCount} 更多属性
          </p>
        )}
        {data.properties.length === 0 && (
          <p className="text-xs text-surface-500 italic">暂无属性</p>
        )}
      </div>

      {/* Actions */}
      {relatedActions.length > 0 && (
        <div className="px-4 pb-3 border-t border-surface-700 pt-2">
          <div className="flex items-center gap-1.5 mb-1.5">
            <BoltIcon className="w-3.5 h-3.5 text-yellow-500" />
            <span className="text-xs text-surface-400 font-medium">动作</span>
          </div>
          <div className="flex flex-wrap gap-1">
            {relatedActions.map((action) => (
              <span
                key={action.id}
                className="px-2 py-0.5 text-xs bg-yellow-500/20 text-[var(--color-accent-action)] rounded-full"
              >
                {action.displayName}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Handles */}
      {HANDLE_SIDES.map(([side, position]) => (
        <span key={side}>
          <Handle
            id={`target-${side}`}
            type="target"
            position={position}
            className={side === 'left' ? VISIBLE_TARGET_HANDLE : INVISIBLE_HANDLE}
          />
          <Handle
            id={`source-${side}`}
            type="source"
            position={position}
            className={side === 'right' ? VISIBLE_SOURCE_HANDLE : INVISIBLE_HANDLE}
          />
        </span>
      ))}
    </div>
  );
});

ObjectTypeNode.displayName = 'ObjectTypeNode';

export default ObjectTypeNode;
