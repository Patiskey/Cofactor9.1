# Cofactor9.1 全量清洗与派生视图协议

## 结论

这 5,337 条不能被清洗成一个“先强制单标签、再去重、再删除歧义”的普通分类集。
那样会系统性删掉真实的 UniProt cofactor 生物学。正确做法是保留一个不丢记录的
`Master-5337`，再从它机械派生不同评估视图：

- `Full-Structured` 是主 benchmark，保留全部 5,337 条和 AND/OR 公式。
- `Core-Provisional` 是传统单标签次级视图，目前有 3,233 条；它不是主数据集。
- `Ambiguity-Challenge` 有 2,082 条，用来单独报告结构、scope、本体和重复冲突难题。

“单标签、去重、无歧义”因此不是三道删除条件，而只是派生视图中的三个维度。
真正的全量清洗至少包含证据、公式、scope、本体、重复、序列字符、note 语义、
冲突裁决和模型泄漏隔离九个相互独立的合同。

## 数据权威与闭合边界

- UniProt release：`2026_02`。
- 冻结查询返回：7,008 条。
- cofactor 对象自身带 `ECO:0000269` 的 accession：5,337 条。
- 不满足对象级实验标签合同的 source candidate：1,671 条；逐条保留拒绝原因。
- 冻结 ChEBI：release 254，目标标签 104 个。
- `Master-5337` SHA-256：
  `6ff828f4d93175ea48bce228b2ee5921118ed358a69e9e8dcf071d82193d7f4f`。

任何筛选只能产生带 reason code 的 derived view，不能从 Master 静默删除 accession，
也不能修改冻结 raw snapshot。

## 九层清洗合同

### 1. 对象级证据准入

实验金标只由 cofactor 对象本身携带的 `ECO:0000269` 产生。entry、note 或其他对象
上的证据不能“借给”cofactor 标签。接受两种可审计来源：

- `source=PubMed`；
- 能在同一 UniProt entry 的 `Reference` 中解析到的引用。

真实全量分布为 5,210 条全 direct PubMed、3 条 mixed direct/reference、124 条全
reference-only。reference-only 是风险分层，不等于伪标签或自动删除。

### 2. 原始 occurrence 与标签投影分离

5,337 条共有 6,981 个实验标签 occurrence、6,974 个唯一
`accession × experimental ChEBI` 对，因而存在 7 个重复 occurrence。原始 occurrence、
顺序、证据和 scope 全保留；去重只发生在用于评分的确定性标签投影中。

### 3. 保留 UniProt block 逻辑

同一 cofactor block 内的标签是可替代关系 `OR`，不同 block 是共同需要关系 `AND`：

```text
AND(block_1, block_2, ...)
block_i = OR(label_1, label_2, ...)
```

全量真实形状为：

- `SINGLE` block 公式：4,178；
- `PURE_OR`：647；
- `PURE_AND`：449；
- `MIXED_AND_OR`：63。

另一个不同口径是“唯一实验 ChEBI 数量为 1”，其数量仍是 4,179；二者相差的 1 条
是两个相同单标签 block 共同出现，公式应是 AND 而不是 SINGLE。因此 1,158 条含多个
唯一实验标签的记录不是“脏数据”。评分投影只能在 block 内去重并做稳定
排序；不得合并两个相同 block，也不得用子集 block “吸收”超集 block。全量应保留
5,911 个非空实验 block。6 个 accession 存在跨 block 标签重叠，共 8 个相交 block
pair；它们证明 separate block 的 multiplicity/role 不能由普通集合悄悄抹掉。

### 4. 保留 molecule / isoform / chain scope

标签不能脱离其 `molecule` 范围。scope 与当前提供的 sequence 不一致时只设置 reason
code，并进入 Challenge/人工裁决；不能把 chain/isoform 标签无条件提升成整条序列金标。

### 5. ChEBI 精确标签与层级分开

原始 ChEBI ID 是严格金标，不能把祖先粗粒度词静默替换为某个后代。104 个目标中有
59 个 target-to-target 祖先关系，涉及 44 个 term，11 个目标本身是其他目标的祖先。
严格 ChEBI 是排名指标；层级部分分只作为 diagnostic。

### 6. exact duplicate 不删除 accession

以 `sha256(sequence)` 定义 sequence entity。全量共有 5,295 个 sequence entity、39 个
exact-duplicate group、81 个 accession：

- 相同序列且相同公式：保留所有 accession，但主指标中整个 entity 总权重为 1；
- 相同序列但公式冲突：6 组、12 条 accession，全部保留并标记
  exact-sequence status `DUPLICATE_CONFLICT` 与 reason code
  `EXACT_SEQUENCE_LABEL_CONFLICT`，主指标权重为 0，单独报告且保持 `PENDING`；
- 不能在冲突组中自动“选一边作为正确答案”。

### 7. near homology 用于加权，不用于删除

DIAMOND 2.2.5 按 identity >= 90% 且双向 coverage >= 80% 冻结聚类。结果为 5,066
个 cluster，其中 4,840 个 singleton、226 个 multi-member，最大 cluster 为 6 条。
所有 39 个 exact-duplicate entity 均未被拆开。cluster-weighted 指标是去冗余诊断，
不改变任何 accession 的存在性或金标。

### 8. 序列字符与 note 风险分开处理

- `U` 是合法 selenocysteine，Master 中 11 条，Core-Provisional 中保留 7 条；不能删。
- `X` 表示未知 residue，Master 中 8 条；可排除出 Core，但仍保留在 Full/Challenge。
- note 只能触发 additive reason code 和 `PENDING`，不能创造、删除或改写金标。
- 冻结规则无法复现历史“541 条 note 风险”口径；当前可复现实测为 477、538、560、
  542 四种明确定义的统计。不能调 regex 或硬编码成员去凑 541。

### 9. 模型输入与 gold 完全隔离

sequence-only case 只含 opaque `sample_id`、amino-acid sequence 和相同的 104 项命名
ChEBI catalog。`sample_id` 由 sequence SHA-256 与同序列组内 ordinal 做 domain-separated
SHA-256 派生，不再由 accession 或公开 HMAC seed 派生，因此不携带序列之外的 accession
信息。不得出现 accession、organism、EC、UniProt note、evidence、PMID、频率、gold label
或 case-specific catalog。私有映射只供离线评分，不能进入 runner。

当前 response 是唯一 ChEBI ID 的无序集合，不能表达同一 ChEBI 在多个独立 block
中的 role/multiplicity。P0ABJ9、Q6AYK3、Q57580、Q8NFF5、Q9LNJ9、Q9SIY3 仍完整
保留并接受模型调用，但在所有正式排名权重中为 0，单列为 overlap/unrepresentable
slice。给这 6 条零权重不是删除数据，而是拒绝用表达能力不足的 response schema 制造
必错或有偏的 headline。

## 三个视图的精确定义

### Full-Structured（5,337，主结果）

不以单标签、note、scope、本体粗细或近同源为删除条件。输出完整 block formula，按
exact-sequence entity macro 作 headline，并补 accession、label、homology-cluster 三种
诊断权重。12 条 exact-sequence conflict accession 与 6 条 response-schema 无法表达的
overlap accession 互不相交，共 18 条在所有正式排名中的权重为 0；有效 accession、
exact-sequence entity、homology cluster 分别为 5,319、5,283、5,056。18 条仍全部运行并
进入独立诊断切片，Core-Provisional 中没有任何零权记录。

### Single-Clean（3,971，中间检查点）

要求实验标签投影和全部 cofactor 标签投影都严格为一个标签。它覆盖 3,942 个 exact
sequence entity 和 64 个标签，但不代表 note/scope/leaf/sequence-quality 都已裁决。

### Core-Provisional（3,233，次级结果）

在 accession-local quality predicate 通过后，才在 non-conflicting exact-sequence group
内选 lexicographically first 的代表。要求 single-clean、leaf target、scope 匹配、无
`X`、无当前规则捕获的 unresolved note risk。名称必须保留 `Provisional`，直到人工 note
裁决完成；它不能替代 Full-Structured headline。

### Ambiguity-Challenge（2,082）

收纳公式结构、非实验附加标签、reference-only 风险、scope、note、本体祖先、`X`、
annotation contradiction 和 duplicate conflict 等情况。一个 accession 可以因多个
reason code 进入该视图；它不是垃圾桶，也不一定与 Core 构成简单二分。

## “95% 清洗把握”的操作性门槛

这里的 95% 不是凭感觉给出的概率，也不是抽 48 条的置信区间。达到可执行清洗的门槛
需要同时满足：

1. 7,008 source candidates 全量闭合为 5,337 accepted + 1,671 reason-coded rejected；
2. 5,337 accession、104 labels、6,981 occurrences、5,911 preserved blocks可重复；
3. 公式四种形状、evidence 三种状态、U/X、ontology、exact duplicate 全量枚举闭合；
4. 5,337 条在 Full/Core/Challenge 中的 membership 与 reason code 可由纯函数重建；
5. public case 与 private gold map 数量、sequence SHA、opaque ID 一一闭合且无泄漏；
6. 手算 fixture、全量 frozen integration 和独立审查均通过；
7. 剩余不确定性被显式限定在 `PENDING` 队列，而不是由脚本偷偷替人裁决。

当前数据构建已经满足前六项的机械闭合；第七项明确保留了 6 个重复冲突组和 note
语义队列。因此可以高把握执行可逆、可审计的派生，而不能声称 3,233 条
Core-Provisional 已经完成所有人工生物学裁决。

## 冻结产物

本协议废止了旧的 5,904-block/v2 派生物。当前全量重建使用
`cofactor9.1.formula.v2` 与 `cofactor9.1.views.v3`，冻结 SHA-256 为：

- Master：`6ff828f4d93175ea48bce228b2ee5921118ed358a69e9e8dcf071d82193d7f4f`
- Full-Structured：`57cf6b5c74de55306b9bbc623514aabdfed6919f291e92ec8ddcfc21599a44fa`
- Single-Clean：`195a59d1a1bd945bdbcdb9cc0045fdd0387716887737cfc15936944485d936d1`
- Core-Provisional：`bed3532d0e4ad332e125df90a02b2b6bba4239f342743367c547cf39545d56bd`
- Ambiguity-Challenge：`e773520fe6df1a2c710dba2e890a8b2eb83398bb2ee593b2af6219745af05278`
- 104-label catalog：`22f79b263ed3b027da2bcc8cc4c475a38a56393cb5dd6a6b7590b1ac979bc234`
- public cases v4：`428a04d7ca720aaefb3d46b422a24b4562843febc6352ebf02cea9d24030d36a`
- public cases v4 manifest：`8c2df43e1e6aba7b06431433811699274e194f135a4f09b385365e4f2e8e0a3f`
- private scoring map（本地 0600，不发布）：
  `4aa267763c2c5a7e9e4d998d7b3d8a4557379afaf5ea3863061de8b9a34b1284`
- homology clusters v3：`330cf792ffe6b2c8888a1cc3bf6b2865c420f90e42d78ccd47fff3897cc91671`
- homology clusters v3 manifest：
  `64072e9e7cff3b2bdf9c2c42a72b996ff1e70833eed4f6a97680c82cdf6d2c11`

改变任何生物学规则都必须产生新 rule version、新产物和新审计记录，不能把旧结果
冒充成新口径。
