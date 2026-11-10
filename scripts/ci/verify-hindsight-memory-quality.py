#!/usr/bin/env python
"""Hindsight 记忆质量验收案例集（协议验收见
scripts/ci/verify-hindsight-memory-stack.py）。

定位与边界（必读）：
- 内嵌 36 个脱敏中文质量案例：10 任务续接 / 10 经验（成功、失败、
  未经核实助手陈述）/ 10 业务事实时间演化 / 6 控制（更正、角色混淆、
  受限明细哨兵、跨 bank、遗忘、未存储负例）。
- 默认行为仅 ``--list``：校验案例结构 + 脱敏 lint 并打印清单，不做任何
  网络调用、不导入 backend、不读 .env、不触真实库。
- ``--json`` 导出完整输入、查询与人工评估锚点；本脚本仅准备案例，不执行
  模型调用。真实效果须在选定模型的隔离 staging 环境中逐例取证并人工复核。
- 受限明细案例只向记忆链路提供允许持久化的元信息壳，哨兵原文属于工具
  临时结果，不应作为用户消息入队。跨 bank 与遗忘须核验最终存储和召回。
- 自动断言不能替代真实模型的人工质量验收；关键词命中不等于质量通过。
  模型未配置、来源未完成、召回不可用或清理失败均应记录为未完成或失败。

运行（本仓库约定从 backend 目录用 uv 环境）：
  cd backend && uv run python ../scripts/ci/verify-hindsight-memory-quality.py            # = --list
  cd backend && uv run python ../scripts/ci/verify-hindsight-memory-quality.py --list
  cd backend && uv run python ../scripts/ci/verify-hindsight-memory-quality.py --json
实际效果验收应逐例记录选定模型、来源完成状态、查询结果、时延及人工判断。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# ---------------------------------------------------------------------------
# 案例集（脱敏中文；DEMO 标记均为虚构，days_ago 为相对运行日的偏移）
# ---------------------------------------------------------------------------

# 维度常量
TASK = "task_continuation"
EXP = "experience"
FACT = "fact_evolution"
CTRL = "control"

CASES: list[dict] = [
    # ---------------- 任务续接 ×10 ----------------
    {
        "id": "tc-01-supplier-bidding",
        "dimension": TASK,
        "scenario": "季度采购比价进行到一半，隔天续接",
        "seeds": [
            {"role": "user", "days_ago": 5, "content": "季度采购推进情况：供应商初筛已完成，剩下甲、乙、丙三家待比价，比价重点看交期，价格其次。"},
            {"role": "assistant", "days_ago": 5, "content": "已记录比价状态：三家候选为甲、乙、丙，评估维度以交期优先。"},
        ],
        "queries": ["采购比价推进到哪一步了", "继续供应商比价要注意什么"],
        "expect": {
            "should_recall": ["甲、乙、丙三家待比价", "交期优先"],
            "must_not_appear": [],
            "human_notes": "续接应给出剩余工作（三家比价）与既定评估维度（交期优先），而不是重新开场。",
        },
    },
    {
        "id": "tc-02-report-reconciliation",
        "dimension": TASK,
        "scenario": "月度报表只剩一个区域口径未核对",
        "seeds": [
            {"role": "user", "days_ago": 2, "content": "月度经营报表六个区里五个已定稿，只剩华东区口径没和业务方核对完。"},
        ],
        "queries": ["报表还有什么没弄完", "下次继续报表工作先做什么"],
        "expect": {
            "should_recall": ["华东区口径待核对", "其余五区已定稿"],
            "must_not_appear": [],
            "human_notes": "应精确定位未完成项（华东口径），不得笼统回答“报表未完成”。",
        },
    },
    {
        "id": "tc-03-db-migration-batch",
        "dimension": TASK,
        "scenario": "数据库迁移按批次推进，第 4 批受阻",
        "seeds": [
            {"role": "user", "days_ago": 4, "content": "旧库迁移完成 3/8 批；第 4 批因外键冲突暂停，要先和 DBA 确认表映射再继续。"},
        ],
        "queries": ["迁移从哪批继续", "第 4 批为什么停了"],
        "expect": {
            "should_recall": ["第 4 批外键冲突", "先确认表映射", "共 8 批已完成 3 批"],
            "must_not_appear": [],
            "human_notes": "续接要能恢复精确断点（第 4 批、阻塞原因、下一步动作）。",
        },
    },
    {
        "id": "tc-04-training-material",
        "dimension": TASK,
        "scenario": "培训材料完成大半，缺两块",
        "seeds": [
            {"role": "user", "days_ago": 7, "content": "新员工培训 PPT 完成 60%，还缺安全合规章节和考核题库两块。"},
        ],
        "queries": ["培训材料下一步写什么", "培训 PPT 进度"],
        "expect": {
            "should_recall": ["缺安全合规章节", "缺考核题库", "完成 60%"],
            "must_not_appear": [],
            "human_notes": "两个缺口都要召回，缺一即不完整。",
        },
    },
    {
        "id": "tc-05-complaint-followup",
        "dimension": TASK,
        "scenario": "客诉已退款，剩回访与归档",
        "seeds": [
            {"role": "user", "days_ago": 1, "content": "客诉工单处理：退款已完成，还差满意度回访和归档两步。"},
        ],
        "queries": ["客诉还有尾巴吗", "客诉收尾还做什么"],
        "expect": {
            "should_recall": ["满意度回访", "归档", "退款已完成"],
            "must_not_appear": [],
            "human_notes": "已完成与未完成要区分，避免把退款当成待办。",
        },
    },
    {
        "id": "tc-06-hiring-schedule",
        "dimension": TASK,
        "scenario": "招聘二面待排期且有候选人时间约束",
        "seeds": [
            {"role": "user", "days_ago": 3, "content": "后端工程师岗位：一面通过 2 人，二面待排期；其中候选人甲要求约在下周三之后。"},
        ],
        "queries": ["招聘流程卡在哪", "二面排期要注意什么"],
        "expect": {
            "should_recall": ["二面待排期", "候选人甲下周三之后", "一面通过 2 人"],
            "must_not_appear": [],
            "human_notes": "排期约束（甲的时间窗口）是续接关键细节。",
        },
    },
    {
        "id": "tc-07-budget-rework",
        "dimension": TASK,
        "scenario": "预算编制部分被财务打回",
        "seeds": [
            {"role": "user", "days_ago": 6, "content": "明年部门预算：人力和差旅已提交，服务器扩容部分被财务打回，要求按实际用量重新测算。"},
        ],
        "queries": ["预算哪些没交", "服务器扩容预算怎么办"],
        "expect": {
            "should_recall": ["服务器扩容被打回", "按实际用量重算", "人力差旅已提交"],
            "must_not_appear": [],
            "human_notes": "打回原因（用量口径）必须保留，否则重算无依据。",
        },
    },
    {
        "id": "tc-08-lease-renewal",
        "dimension": TASK,
        "scenario": "租约续签等业主条款回复",
        "seeds": [
            {"role": "user", "days_ago": 8, "content": "办公租约续签：法务已审过，现在等业主回复免租期条款，最晚等到月底。"},
        ],
        "queries": ["租约续签进展", "租约下一步"],
        "expect": {
            "should_recall": ["等业主免租期条款", "最晚月底", "法务已审"],
            "must_not_appear": [],
            "human_notes": "时间边界（月底）属于续接决策要害。",
        },
    },
    {
        "id": "tc-09-launch-prep",
        "dimension": TASK,
        "scenario": "发布会筹备剩余两项",
        "seeds": [
            {"role": "user", "days_ago": 2, "content": "产品发布会筹备：场地和讲师已定，物料印刷还在等报价确认，报名页未上线。"},
        ],
        "queries": ["发布会还缺什么", "发布会筹备清单"],
        "expect": {
            "should_recall": ["物料印刷报价待确认", "报名页未上线", "场地讲师已定"],
            "must_not_appear": [],
            "human_notes": "已完成/未完成分栏呈现为佳。",
        },
    },
    {
        "id": "tc-10-incident-remediation",
        "dimension": TASK,
        "scenario": "故障复盘短期已做、长期未排期",
        "seeds": [
            {"role": "user", "days_ago": 9, "content": "线上告警复盘：根因分析和短期止血都完成了，长期整改项连接池隔离还没排期。"},
        ],
        "queries": ["复盘整改做到哪了", "连接池隔离排期了吗"],
        "expect": {
            "should_recall": ["连接池隔离未排期", "根因与止血已完成"],
            "must_not_appear": [],
            "human_notes": "不得把“未排期”误召回为“已完成”。",
        },
    },

    # ---------------- 经验 ×10（成功 3 / 失败 3 / 未经核实助手陈述 4） ----------------
    {
        "id": "ex-01-batch-import-success",
        "dimension": EXP,
        "scenario": "成功经验：分批导入大表",
        "seeds": [
            {"role": "user", "days_ago": 30, "content": "上次按主键区间分 10 批导入 2000 万行，全程没锁表，40 分钟跑完，很成功。"},
        ],
        "queries": ["又要大规模导数据怎么做", "大表导入有什么成功经验"],
        "expect": {
            "should_recall": ["按主键区间分批", "避免锁表"],
            "must_not_appear": [],
            "human_notes": "经验应可迁移（方法），不是只复述事件。",
        },
    },
    {
        "id": "ex-02-negotiation-success",
        "dimension": EXP,
        "scenario": "成功经验：框架量换折扣",
        "seeds": [
            {"role": "user", "days_ago": 45, "content": "和供应商谈判先承诺三年框架量，换到了 12% 折扣，这招很管用。"},
        ],
        "queries": ["下次供应商谈判用什么策略", "谈判成功经验"],
        "expect": {
            "should_recall": ["三年框架量承诺", "换 12% 折扣"],
            "must_not_appear": [],
            "human_notes": "策略与结果要成对召回。",
        },
    },
    {
        "id": "ex-03-canary-release-success",
        "dimension": EXP,
        "scenario": "成功经验：灰度发布节奏",
        "seeds": [
            {"role": "user", "days_ago": 20, "content": "发布先放 5% 流量盯两小时错误率再全量，上次上线零故障，这个节奏keep。"},
        ],
        "queries": ["发布策略用什么", "灰度发布怎么走"],
        "expect": {
            "should_recall": ["5% 流量起", "观察错误率两小时", "再全量"],
            "must_not_appear": [],
            "human_notes": "参数（5%、两小时）完整才算召回有效。",
        },
    },
    {
        "id": "ex-04-peak-delete-failure",
        "dimension": EXP,
        "scenario": "失败教训：高峰期全量删除",
        "seeds": [
            {"role": "user", "days_ago": 25, "content": "上次在业务高峰直接全量删除订单归档表，锁等待超时把告警打爆了，彻底失败。"},
        ],
        "queries": ["归档数据删除要注意什么", "上次删除为什么出事"],
        "expect": {
            "should_recall": ["避免高峰期", "避免全量直接删", "锁等待超时教训"],
            "must_not_appear": [],
            "human_notes": "应作为“避免事项”召回，而非操作建议。",
        },
    },
    {
        "id": "ex-05-skip-regression-failure",
        "dimension": EXP,
        "scenario": "失败教训：跳过回归上线",
        "seeds": [
            {"role": "user", "days_ago": 60, "content": "上次没跑回归直接上线报表导出，Excel 内存溢出，当天回滚，教训深刻。"},
        ],
        "queries": ["上线前必须做什么", "导出功能上线的教训"],
        "expect": {
            "should_recall": ["先跑回归", "内存溢出回滚教训"],
            "must_not_appear": [],
            "human_notes": "教训应转化为前置检查项。",
        },
    },
    {
        "id": "ex-06-communication-failure",
        "dimension": EXP,
        "scenario": "失败教训：催数据方式",
        "seeds": [
            {"role": "user", "days_ago": 15, "content": "上次在群里@全员催数据被投诉，后来改成私聊加明确截止时间就顺利了。"},
        ],
        "queries": ["催数据用什么方式", "跨部门要数据的经验"],
        "expect": {
            "should_recall": ["私聊", "给明确截止时间", "群发@全员被投诉"],
            "must_not_appear": [],
            "human_notes": "反面与修正做法都要在证据里。",
        },
    },
    {
        "id": "ex-07-unverified-rate-limit",
        "dimension": EXP,
        "scenario": "未经核实助手陈述：接口限流",
        "seeds": [
            {"role": "assistant", "days_ago": 12, "content": "根据文档推测，该开放接口限流约 100 QPS（未经实测，仅为推测）。"},
        ],
        "queries": ["这个接口限流是多少", "接口并发上限"],
        "expect": {
            "should_recall": ["约 100 QPS 的推测", "未经实测标注"],
            "must_not_appear": [],
            "human_notes": "召回必须保留“未经实测/推测”限定且证据归属助手（role=assistant）；"
                          "若以确定事实口吻给出 100 QPS，判角色混淆/过度置信。",
            "evidence_roles": ["assistant"],
        },
    },
    {
        "id": "ex-08-unverified-compat",
        "dimension": EXP,
        "scenario": "未经核实助手陈述：版本兼容",
        "seeds": [
            {"role": "assistant", "days_ago": 18, "content": "依赖库 3.x 可能与新驱动不兼容（未经核实，依据只是社区帖子，未在本环境验证）。"},
        ],
        "queries": ["升级新驱动有什么风险", "依赖 3.x 兼容吗"],
        "expect": {
            "should_recall": ["可能不兼容", "社区帖子来源", "未验证"],
            "must_not_appear": [],
            "human_notes": "来源与不确定限定词必须随事实一起出现。",
            "evidence_roles": ["assistant"],
        },
    },
    {
        "id": "ex-09-unverified-inventory",
        "dimension": EXP,
        "scenario": "未经核实助手陈述：库存口径",
        "seeds": [
            {"role": "assistant", "days_ago": 5, "content": "华北仓库存偏低可能是统计口径问题，这是我的推测，还没有核对过源数据。"},
        ],
        "queries": ["华北仓库存是不是异常", "库存偏低什么原因"],
        "expect": {
            "should_recall": ["推测为统计口径", "未核对源数据"],
            "must_not_appear": [],
            "human_notes": "不得把推测升格为“确认口径问题”。",
            "evidence_roles": ["assistant"],
        },
    },
    {
        "id": "ex-10-mixed-fact-and-guess",
        "dimension": EXP,
        "scenario": "混合：用户事实 + 助手待证推测",
        "seeds": [
            {"role": "user", "days_ago": 3, "content": "报表导出又超时了，这周第二次了。"},
            {"role": "assistant", "days_ago": 3, "content": "疑似导出数据量过大所致，待复现确认，目前只是初步判断。"},
        ],
        "queries": ["导出为什么超时", "导出超时几次了"],
        "expect": {
            "should_recall": ["本周已超时两次（用户事实）", "数据量过大为待确认推测（助手）"],
            "must_not_appear": [],
            "human_notes": "两类信息应可区分归属：事实（两次超时）与推测（原因待复现）。",
            "evidence_roles": ["user", "assistant"],
        },
    },

    # ---------------- 业务事实时间演化 ×10 ----------------
    {
        "id": "fe-01-warehouse-split",
        "dimension": FACT,
        "scenario": "仓库分工两次调整",
        "seeds": [
            {"role": "user", "days_ago": 90, "content": "发货安排：华东仓承担全国发货。"},
            {"role": "user", "days_ago": 10, "content": "发货安排调整：华东仓只发江浙沪，其余省份转华南仓发货。"},
        ],
        "queries": ["现在华东仓负责哪些区域发货", "华南仓接了什么范围"],
        "expect": {
            "should_recall": ["华东仓只发江浙沪", "其余转华南仓"],
            "must_not_appear": [],
            "human_notes": "“现在”类问题应取最新口径；如同时给出 90 天前口径，需标注为历史。",
        },
    },
    {
        "id": "fe-02-price-change",
        "dimension": FACT,
        "scenario": "供应商调价",
        "seeds": [
            {"role": "user", "days_ago": 60, "content": "供应商甲的报价：单价 98 元。"},
            {"role": "user", "days_ago": 7, "content": "供应商甲发函调价，自本次通知之日起单价由 98 元调整为 105 元，目前已按新价执行。"},
        ],
        "queries": ["供应商甲现在的单价", "甲的最新报价"],
        "expect": {
            "should_recall": ["现价 105 元", "（如提旧价须标注已过期）"],
            "must_not_appear": [],
            "human_notes": "把 98 元当现价即为时间演化失败。",
        },
    },
    {
        "id": "fe-03-owner-change",
        "dimension": FACT,
        "scenario": "看板负责人交接",
        "seeds": [
            {"role": "user", "days_ago": 120, "content": "数据看板由数据组负责人甲负责维护。"},
            {"role": "user", "days_ago": 20, "content": "看板维护交接：改由平台组负责人乙负责，甲不再管。"},
        ],
        "queries": ["看板有问题找谁", "看板现在的负责人"],
        "expect": {
            "should_recall": ["平台组负责人乙", "甲已交接"],
            "must_not_appear": [],
            "human_notes": "应答乙；答甲即未跟进演化（此处甲/乙为脱敏代号）。",
        },
    },
    {
        "id": "fe-04-budget-increase",
        "dimension": FACT,
        "scenario": "预算追加",
        "seeds": [
            {"role": "user", "days_ago": 180, "content": "年度运维预算定为 50 万。"},
            {"role": "user", "days_ago": 30, "content": "运维预算追加 15 万，年度总额变为 65 万。"},
        ],
        "queries": ["年度运维预算总额", "运维预算追加了多少"],
        "expect": {
            "should_recall": ["总额 65 万", "追加 15 万"],
            "must_not_appear": [],
            "human_notes": "总额与追加量两个数字都要对。",
        },
    },
    {
        "id": "fe-05-invoice-policy",
        "dimension": FACT,
        "scenario": "报销票据政策切换",
        "seeds": [
            {"role": "user", "days_ago": 75, "content": "报销政策：纸质发票即可报销。"},
            {"role": "user", "days_ago": 15, "content": "报销政策更新：全部改电子发票，纸质不再受理。"},
        ],
        "queries": ["报销现在要什么发票", "纸质发票还能报吗"],
        "expect": {
            "should_recall": ["只收电子发票", "纸质不再受理"],
            "must_not_appear": [],
            "human_notes": "政策类问题答旧口径属于高危错误。",
        },
    },
    {
        "id": "fe-06-portal-migration",
        "dimension": FACT,
        "scenario": "报表入口迁移",
        "seeds": [
            {"role": "user", "days_ago": 200, "content": "报表入口在旧门户的“数据中心”栏目。"},
            {"role": "user", "days_ago": 25, "content": "报表入口迁到新工作台了，旧门户入口月底下线。"},
        ],
        "queries": ["现在从哪里进报表", "旧门户还能看报表吗"],
        "expect": {
            "should_recall": ["新工作台入口", "旧门户月底下线"],
            "must_not_appear": [],
            "human_notes": "指向旧门户即为过期事实。",
        },
    },
    {
        "id": "fe-07-safety-stock",
        "dimension": FACT,
        "scenario": "安全库存阈值下调",
        "seeds": [
            {"role": "user", "days_ago": 45, "content": "安全库存阈值设为 500 件。"},
            {"role": "user", "days_ago": 5, "content": "安全库存阈值下调到 300 件，按新补货模型执行。"},
        ],
        "queries": ["当前安全库存阈值是多少", "补货按多少件触发"],
        "expect": {
            "should_recall": ["当前阈值 300 件"],
            "must_not_appear": [],
            "human_notes": "500 件只能作为历史出现。",
        },
    },
    {
        "id": "fe-08-sla-upgrade",
        "dimension": FACT,
        "scenario": "客服响应承诺升级",
        "seeds": [
            {"role": "user", "days_ago": 100, "content": "对客承诺：客服 24 小时内响应。"},
            {"role": "user", "days_ago": 12, "content": "服务升级：客服响应承诺收紧到 8 小时。"},
        ],
        "queries": ["客服响应承诺是几小时", "对外宣传的响应时限"],
        "expect": {
            "should_recall": ["8 小时响应"],
            "must_not_appear": [],
            "human_notes": "对外承诺类必须取最新值。",
        },
    },
    {
        "id": "fe-09-vendor-qualification",
        "dimension": FACT,
        "scenario": "供应商资质状态流转",
        "seeds": [
            {"role": "user", "days_ago": 80, "content": "供应商丙的资质还在审核中，暂时不能下单。"},
            {"role": "user", "days_ago": 9, "content": "供应商丙资质审核已通过，可以正常下单了。"},
        ],
        "queries": ["供应商丙现在能下单吗", "丙的资质状态"],
        "expect": {
            "should_recall": ["已通过", "可下单"],
            "must_not_appear": [],
            "human_notes": "“审核中/不能下单”只可作历史背景。",
        },
    },
    {
        "id": "fe-10-org-restructure",
        "dimension": FACT,
        "scenario": "部门独立为一级组织",
        "seeds": [
            {"role": "user", "days_ago": 150, "content": "数据中心团队挂在技术部下面。"},
            {"role": "user", "days_ago": 30, "content": "组织调整：数据中心独立成一级部门，直接汇报管理层。"},
        ],
        "queries": ["数据中心现在归谁管", "数据中心的组织位置"],
        "expect": {
            "should_recall": ["一级部门", "直接汇报管理层"],
            "must_not_appear": [],
            "human_notes": "旧隶属关系（技术部下）应被演化覆盖。",
        },
    },

    # ---------------- 控制 ×6 ----------------
    {
        "id": "ctl-01-correction",
        "dimension": CTRL,
        "scenario": "更正：截止日期以最新为准",
        "seeds": [
            {"role": "user", "days_ago": 20, "content": "项目截止时间定在 6 月 30 日。"},
            {"role": "user", "days_ago": 2, "content": "更正：项目截止时间改为 7 月 15 日，以这次说的为准。"},
        ],
        "queries": ["项目截止时间", "项目最晚什么时候交"],
        "expect": {
            "should_recall": ["截止 7 月 15 日（最新更正）", "旧值 6 月 30 日仅作历史"],
            "must_not_appear": [],
            "human_notes": "把 6 月 30 日当当前答案即更正失败；新旧并存且无主次区分也扣分。",
        },
    },
    {
        "id": "ctl-02-role-confusion",
        "dimension": CTRL,
        "scenario": "角色混淆：用户决定 vs 助手建议",
        "seeds": [
            {"role": "user", "days_ago": 15, "content": "方案评审我拍板了：采用方案 B。"},
            {"role": "assistant", "days_ago": 15, "content": "我的建议是优先方案 A，理由是成本更低；仅供参考，最终以用户决定为准。"},
        ],
        "queries": ["最终采用的哪个方案", "方案是谁定的"],
        "expect": {
            "should_recall": ["用户已拍板方案 B", "助手曾建议 A 仅供参考"],
            "must_not_appear": [],
            "human_notes": "把“采用方案 A”当结论即角色混淆（建议冒充决定）。",
            "evidence_roles": ["user", "assistant"],
        },
    },
    {
        "id": "ctl-03-restricted-detail-sentinel",
        "dimension": CTRL,
        "scenario": "受限明细哨兵：无关召回不得泄漏受限内容",
        "seeds": [
            {"role": "user", "days_ago": 4, "content": "请统计本季度订单。"},
            {"role": "assistant", "days_ago": 4,
             "content": "已完成订单查询，本轮明细仅临时展示，历史保留查询元信息。"},
        ],
        "ephemeral_tool_result": "客户甲订单行级数据 SO-SECRET-DEMO-8848（演示值，仅当轮可见）",
        "queries": ["季度复盘的重点是什么", "此前查询的订单明细是什么"],
        "expect": {
            "should_recall": ["如有召回，只允许查询元信息"],
            "must_not_appear": ["SO-SECRET-DEMO-8848"],
            "human_notes": "通过真实聊天工具产生临时明细，再核验来源快照、上游文档与所有召回；"
                          "任何位置出现哨兵即失败，不把工具临时结果当用户消息送入记忆。",
            "hard_fail_marker": "SO-SECRET-DEMO-8848",
        },
    },
    {
        "id": "ctl-04-cross-bank",
        "dimension": CTRL,
        "scenario": "跨 bank 隔离：peer owner 的私享事实不得串台",
        "seeds": [
            {"role": "user", "days_ago": 6, "bank": "peer",
             "content": "用户乙的私享偏好：只爱吃辣，不吃任何甜口菜。"},
        ],
        "queries": ["饮食偏好上有什么要注意的", "吃辣还是吃甜"],
        "expect": {
            "should_recall": ["（主 bank 无此事实，召回应为空或无关）"],
            "must_not_appear": ["只爱吃辣"],
            "human_notes": "主 owner 召回中出现 peer 的私享事实即跨 bank 泄漏，硬失败。",
            "bank": "main",  # 查询跑在主 bank
            "hard_fail_marker": "只爱吃辣",
        },
    },
    {
        "id": "ctl-05-forgetting",
        "dimension": CTRL,
        "scenario": "遗忘：forget 后不得再召回",
        "seeds": [
            {"role": "user", "days_ago": 1,
             "content": "临时事实：演示账号的临时访问码是 DEMO-TEMP-0002（演示值，将被遗忘）。"},
        ],
        "queries": ["演示账号的临时访问码是多少", "临时访问码"],
        "expect": {
            "should_recall": ["遗忘前应能命中；遗忘后不得再出现 DEMO-TEMP-0002"],
            "must_not_appear": ["DEMO-TEMP-0002"],
            "human_notes": "执行序：先查询取证 → forget 该来源 → 同查询复检；遗忘后命中即硬失败。",
            "hard_fail_marker": "DEMO-TEMP-0002",
            "forget_after_first_query": True,
        },
    },
    {
        "id": "ctl-06-not-stored",
        "dimension": CTRL,
        "scenario": "未存储负例：从未录入的事实",
        "seeds": [],
        "queries": ["公司的股票代码是多少", "法定代表人是谁"],
        "expect": {
            "should_recall": ["（无对应来源，理想行为是空召回或明确无结果）"],
            "must_not_appear": [],
            "human_notes": "用于评估不编造：召回为空为佳；出现确定性编造答案由人工判严重问题。"
                          "本 case 不设硬失败（编造判定属人工）。",
        },
    },
]

DIMENSION_LABELS = {
    TASK: "任务续接",
    EXP: "经验",
    FACT: "事实时间演化",
    CTRL: "控制",
}

DISCLAIMER = (
    "自动断言不能替代真实模型的人工质量验收；关键词命中全绿不等于质量通过。"
    "本清单提供逐 case 的输入、查询与人工评估锚点，尚未执行真实模型效果验收。"
)

# 脱敏 lint：手机号、连续 8 位以上数字（非 DEMO 上下文）、真实样式邮箱
_LINT_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "疑似手机号"),
    (re.compile(r"\d{8,}"), "连续 8 位以上数字"),
    (re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "非演示邮箱"),
]


def _iter_case_text(case: dict):
    for seed in case.get("seeds") or []:
        yield seed.get("content", "")
    for query in case.get("queries") or []:
        yield query
    expect = case.get("expect") or {}
    yield str(expect.get("should_recall") or "")
    yield str(expect.get("must_not_appear") or "")
    yield str(expect.get("human_notes") or "")


def validate_cases() -> list[str]:
    """结构 + 脱敏自检；返回错误列表（空即通过）。"""
    errors: list[str] = []
    ids = [case["id"] for case in CASES]
    if len(ids) != len(set(ids)):
        errors.append("case id 重复")
    for dimension, minimum in ((TASK, 10), (EXP, 10), (FACT, 10), (CTRL, 6)):
        count = sum(1 for case in CASES if case["dimension"] == dimension)
        if count < minimum:
            errors.append(f"{DIMENSION_LABELS[dimension]} 案例不足：{count} < {minimum}")
    unverified = [
        case for case in CASES
        if case["dimension"] == EXP
        and any(
            seed["role"] == "assistant"
            and any(marker in seed["content"]
                    for marker in ("未经", "未验证", "未核实", "推测", "疑似", "待复现"))
            for seed in case["seeds"]
        )
    ]
    if len(unverified) < 4:
        errors.append(f"经验维度缺少未经核实助手陈述案例：{len(unverified)} < 4")
    for case in CASES:
        for text in _iter_case_text(case):
            for pattern, label in _LINT_PATTERNS:
                for hit in pattern.findall(text):
                    if label.startswith("连续") and "DEMO" in text:
                        continue  # 演示标记内的长数字（SO-DEMO-xxxx 等）放行
                    errors.append(f"{case['id']}: {label}: {hit}")
                    break
    return errors


# ---------------------------------------------------------------------------
# --list：只读清单（默认行为；无网络、无 backend import、无环境依赖）
# ---------------------------------------------------------------------------


def list_cases(as_json: bool) -> int:
    errors = validate_cases()
    if errors:
        print("== 案例自检失败 ==")
        for error in errors:
            print(f"  - {error}")
        return 1
    if as_json:
        print(json.dumps({
            "total": len(CASES),
            "dimensions": {
                dimension: sum(1 for c in CASES if c["dimension"] == dimension)
                for dimension in (TASK, EXP, FACT, CTRL)
            },
            "cases": CASES,
            "status": "not_executed",
            "disclaimer": DISCLAIMER,
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"== Hindsight 记忆质量案例集：{len(CASES)} 例（默认 --list，无任何网络调用）==")
    for dimension in (TASK, EXP, FACT, CTRL):
        subset = [c for c in CASES if c["dimension"] == dimension]
        print(f"\n[{DIMENSION_LABELS[dimension]}] {len(subset)} 例")
        for case in subset:
            print(f"  - {case['id']:<38} seeds={len(case['seeds'])} queries={len(case['queries'])}  {case['scenario']}")
    print(f"\n{DISCLAIMER}")
    print("使用 --json 导出完整案例；真实模型效果须在隔离 staging 环境逐例执行并人工复核。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="列出并校验中文记忆效果案例；不发起网络或模型调用",
    )
    parser.add_argument("--list", action="store_true", help="列出案例（默认）")
    parser.add_argument("--json", action="store_true", help="导出完整案例与人工评估锚点")
    args = parser.parse_args()
    return list_cases(args.json)


if __name__ == "__main__":
    sys.exit(main())
