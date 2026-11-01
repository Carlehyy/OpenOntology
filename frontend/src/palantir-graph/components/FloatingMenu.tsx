import { useState } from 'react';
import {
  Bars3Icon,
  XMarkIcon,
  PlayIcon,
  CommandLineIcon,
  ShieldExclamationIcon,
  TableCellsIcon,
  ClockIcon,
  RocketLaunchIcon,
  CircleStackIcon,
} from '@heroicons/react/24/outline';
import type { GraphWorkspaceCapabilities } from '../workspaceCapabilities';

interface MenuItem {
  id: string;
  label: string;
  icon: React.ElementType;
  color: string;
  bgColor: string;
  onClick: () => void;
  disabledReason?: string;
}

interface FloatingMenuProps {
  onOpenFunctionTest: () => void;
  onOpenActionRun: () => void;
  onOpenSentinel: () => void;
  onOpenInstances: () => void;
  onOpenRunHistory: () => void;
  onOpenAutonomy: () => void;
  onOpenGraphDatabase: () => void;
  capabilities: GraphWorkspaceCapabilities;
}

export const FloatingMenu = ({
  onOpenFunctionTest,
  onOpenActionRun,
  onOpenSentinel,
  onOpenInstances,
  onOpenRunHistory,
  onOpenAutonomy,
  onOpenGraphDatabase,
  capabilities,
}: FloatingMenuProps) => {
  const [isOpen, setIsOpen] = useState(false);
  const runtimeDisabledReason = capabilities.runtimeDisabledReason;

  const menuItems: MenuItem[] = [
    {
      id: 'instances',
      label: '实例数据',
      icon: TableCellsIcon,
      color: 'text-indigo-400',
      bgColor: 'bg-indigo-500/20 hover:bg-indigo-500/30',
      onClick: () => {
        onOpenInstances();
        setIsOpen(false);
      },
      disabledReason: capabilities.canBrowseInstances ? undefined : runtimeDisabledReason,
    },
    {
      id: 'graphdb',
      label: '图数据库视图',
      icon: CircleStackIcon,
      color: 'text-cyan-400',
      bgColor: 'bg-cyan-500/20 hover:bg-cyan-500/30',
      onClick: () => {
        onOpenGraphDatabase();
        setIsOpen(false);
      },
    },
    {
      id: 'runhistory',
      label: '运行历史',
      icon: ClockIcon,
      color: 'text-teal-400',
      bgColor: 'bg-teal-500/20 hover:bg-teal-500/30',
      onClick: () => {
        onOpenRunHistory();
        setIsOpen(false);
      },
      disabledReason: capabilities.canViewRunHistory ? undefined : runtimeDisabledReason,
    },
    {
      id: 'autonomy',
      label: '自治等级',
      icon: RocketLaunchIcon,
      color: 'text-amber-400',
      bgColor: 'bg-amber-500/20 hover:bg-amber-500/30',
      onClick: () => {
        onOpenAutonomy();
        setIsOpen(false);
      },
      disabledReason: capabilities.canManageAutonomy ? undefined : runtimeDisabledReason,
    },
    {
      id: 'sentinel',
      label: '哨兵引擎',
      icon: ShieldExclamationIcon,
      color: 'text-rose-400',
      bgColor: 'bg-rose-500/20 hover:bg-rose-500/30',
      onClick: () => {
        onOpenSentinel();
        setIsOpen(false);
      },
      disabledReason: capabilities.canManageSentinels ? undefined : runtimeDisabledReason,
    },
    {
      id: 'runaction',
      label: '执行动作',
      icon: PlayIcon,
      color: 'text-green-400',
      bgColor: 'bg-green-500/20 hover:bg-green-500/30',
      onClick: () => {
        onOpenActionRun();
        setIsOpen(false);
      },
      disabledReason: capabilities.canRunActions ? undefined : runtimeDisabledReason,
    },
    {
      id: 'testfn',
      label: '测试函数',
      icon: CommandLineIcon,
      color: 'text-pink-400',
      bgColor: 'bg-pink-500/20 hover:bg-pink-500/30',
      onClick: () => {
        onOpenFunctionTest();
        setIsOpen(false);
      },
      disabledReason: capabilities.canTestFunctions ? undefined : runtimeDisabledReason,
    },
  ];

  return (
    <div className="fixed bottom-6 right-6 z-40">
      <div
        aria-hidden={!isOpen}
        className={`
          absolute bottom-16 right-0 flex flex-col gap-2
          transition-all duration-300 origin-bottom-right
          ${isOpen
            ? 'opacity-100 scale-100 pointer-events-auto'
            : 'opacity-0 scale-95 pointer-events-none'
          }
        `}
      >
        {menuItems.map((item, index) => (
          <button
            key={item.id}
            data-testid={`graph-runtime-tool-${item.id}`}
            tabIndex={isOpen ? 0 : -1}
            onClick={item.onClick}
            title={item.disabledReason ? `${item.label}：${item.disabledReason}` : item.label}
            aria-label={item.label}
            data-read-only={item.disabledReason ? 'true' : 'false'}
            className={`
              flex items-center gap-3 px-4 py-2.5 rounded-xl text-left
              bg-surface-800/95 backdrop-blur-sm border border-surface-700/50
              shadow-lg shadow-black/30 hover:shadow-xl
              transition-all duration-200 hover:scale-[1.02]
              ${item.disabledReason ? 'border-surface-600/70 bg-surface-800/80' : ''}
              ${isOpen ? 'translate-y-0' : 'translate-y-4'}
            `}
            style={{
              transitionDelay: isOpen ? `${index * 50}ms` : '0ms',
            }}
          >
            <div className={`p-2 rounded-lg ${item.bgColor}`}>
              <item.icon className={`w-5 h-5 ${item.color}`} />
            </div>
            <span className="max-w-64 whitespace-nowrap">
              <span className="block text-sm font-medium text-surface-100">{item.label}</span>
              {item.disabledReason && (
                <span className="block max-w-60 truncate text-[10px] text-surface-400">
                  仅查看 · {item.disabledReason}
                </span>
              )}
            </span>
          </button>
        ))}
      </div>

      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          w-14 h-14 rounded-full flex items-center justify-center
          shadow-lg transition-all duration-300 hover:scale-105
          ${isOpen
            ? 'bg-surface-700 hover:bg-surface-600 shadow-black/30'
            : 'bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 shadow-indigo-900/40'
          }
        `}
        title={isOpen ? '关闭菜单' : '打开菜单'}
      >
        <div className={`transition-transform duration-300 ${isOpen ? 'rotate-90' : ''}`}>
          {isOpen ? (
            <XMarkIcon className="w-6 h-6 text-white" />
          ) : (
            <Bars3Icon className="w-6 h-6 text-white" />
          )}
        </div>
      </button>

      {isOpen && (
        <div
          className="fixed inset-0 -z-10"
          onClick={() => setIsOpen(false)}
        />
      )}
    </div>
  );
};

export default FloatingMenu;
