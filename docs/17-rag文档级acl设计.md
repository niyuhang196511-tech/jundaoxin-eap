# 17 - L6 RAG 文档级 ACL 设计方案（v1.1 / M36 候选，待批准）

> **状态：设计方案，待用户批准后实施。** 目标：在租户隔离（M23 已落地）之上增加
> **文档/角色级访问控制**，保证「Agent 只检索当前用户有权限的文档」——企业级刚需
> （unfinished.md §十六 Permission-aware RAG 的深化项）。

## 一、目标与非目标

**目标**
1. 按**文档**（及整库默认规则）配置 allow/deny，主体为**角色 / 用户**；
2. 三路检索（BM25 / 向量 / 图谱）与 Citation **同步过滤**——无权限的 chunk 不进入上下文、
   不出现在引用中，且**不泄露计数**（命中数按过滤后计）；
3. 存量零破坏：无 ACL 规则的知识库行为与 v1.0.0 完全一致（租户过滤为唯一边界）；
4. 管理 API（admin + 审计）+ 知识库控制台管理入口。

**非目标（本期不做）**
- chunk 级 ACL（文档级足够覆盖企业场景，chunk 级复杂度陡增）；
- 与连接器/IM 权限体系打通（后续可复用同一 subject 抽象）；
- 跨租户共享 ACL。

## 二、数据模型（新表 `document_acls`，迁移 1 个）

```
DocumentACL:
  id            int PK
  kb_id         int  FK → kbs（级联删除）
  document_id   int | None  FK → documents（None = 该 KB 的默认规则）
  effect        "allow" | "deny"
  subject_type  "role" | "user"
  subject       str（角色名 / 用户 id；"*" = 所有主体）
  note          str
  created_at / updated_at
  索引：(kb_id, document_id), (subject_type, subject)
```

## 三、判定语义（关键设计）

对每个待召回 chunk，取其 document 的规则集（含 KB 级默认规则），按请求主体（`role`
取 JWT claim，API Key 通道视为 `role=admin`）：

```
1. 任一 deny 命中（subject 匹配，含 "*"）            → 拒绝
2. 存在 allow 命中                                    → 允许
3. 无规则（或仅未命中）                               → 允许（默认可见，存量兼容）
```

- **deny 优先**保证紧急下线一条文档不需要枚举 allow；
- **默认可见**保证存量知识库零迁移零破坏（与租户过滤叠加，非替代）；
- 语义写死在 service 层单一函数 `is_allowed(rules, subject) -> bool`（纯函数可测）。

## 四、检索过滤点（单点接入）

`knowledge/service.py` 检索入口：chunk 召回（三路合并 + RRF）之后、**Reranker 之前**
过滤——一处接入三路全生效：

1. 按 (kb_id) 一次性取规则集（进程内 TTL 缓存 30s，规则数企业场景极小）；
2. chunk 按 document_id 分组判定 → 无权限剔除（含图谱检索路径与 citations 收集）；
3. 命中数/总量按过滤后计（不泄露「有多少条被隐藏」）；
4. 审计：检索命中包含 ACL 过滤时记 `kb.acl.filtered`（不含被滤内容，仅计数）。

**主体上下文**：`resolve_tenant` 依赖注入处已解析 user/role（JWT claim）——透传到
检索调用的 `AclContext(tenant_id, user_id, role)`；API Key 通道 = `role="admin"`。

## 五、API（admin + 审计 `kb.acl.*`）

| 端点 | 说明 |
|---|---|
| `GET /api/v1/kb/{name}/acls` | 规则列表（kb 级 + 文档级，带 document 名） |
| `POST /api/v1/kb/{name}/acls` | 新建规则（effect/subject_type/subject/document 可选） |
| `DELETE /api/v1/kb/acls/{acl_id}` | 删除规则 |
| （导入联动）ingest 可选参数 `acl` | 文档导入时携带初始规则 |

前端：Knowledge 页文档行「访问控制」入口（列表 + 增删，复用现有弹窗模式）。

## 六、测试计划（离线确定性）

1. 纯函数：deny 优先 / allow 白名单 / `*` 通配 / 默认可见 / 角色与用户主体；
2. 检索集成：无规则存量兼容（三路 + Citation 与 v1.0.0 一致）；deny 文档不出现在命中
   与引用；allow 白名单模式下未授权不可见；图谱路径同步过滤；
3. 缓存：规则变更后 TTL 内外行为；4. API CRUD + RBAC + 审计；5. 计数不泄露（filtered 计数在审计而非响应）。

## 七、工程量与批次

中等：1 迁移 + 模型 + service 过滤单点 + 4 端点 + 纯函数/集成测试 + 前端管理入口。
预计一个批次（M36，可比照 M34 三线规模）。

## 八、开放问题（请用户批准时一并定）

1. **主体粒度**：本期 role/user 两级是否够？（组/group 与部门体系是否需要——建议本期
   不做，subject 字符串设计已预留扩展）
2. **默认语义**：无规则=可见（我的建议，存量兼容）；若企业倾向「默认不可见、显式授权」，
   可加 KB 级开关 `acl_default_deny`（实现成本 +1 天）——默认我按「默认可见」实施。
3. **org 记忆联动 KB（L7 剩余）**：沉淀产生的文档是否自动带 ACL？建议复用本机制
   （沉淀文档默认 `role=* allow` + 租户过滤），可并入 M36。
