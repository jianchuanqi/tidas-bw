# tidas-bw

`tidas-bw` 是一个独立的命令行工具，用于在本地完成 TIDAS 数据包与 Brightway 之间的双向迁移。Brightway 一侧使用新一代官方包 `bw2data` 4.x（兼容 Brightway 2.5 数据模型）。

它不调用、不修改天工 LCA 平台，也不需要 SQLite 中间库。输入和输出是 TIDAS ZIP/目录，Brightway 一侧通过其公开 Python 接口读写。工具只接受能够明确证明为开放使用的数据。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
cd /Users/jianchuan/Dev/tidas-bw
uv tool install .
tidas-bw --help
```

在源码目录开发：

```bash
uv sync --python 3.12 --extra dev
uv run tidas-bw --help
```

## 主要命令

先检查 TIDAS ZIP 或解压目录（只查格式、许可和引用闭包，不判断能否导入）：

```bash
tidas-bw validate tiangong-open-data.zip --strict-references
```

导入前完整预检——运行数据与映射检查（格式、许可、引用、数值精度、不确定性、供应者解析；目标项目中的重名和替换依赖另在实际导入时检查），**绝不创建或修改任何 Brightway 数据**，并在报告中区分“包校验通过”与“可映射”两类结论：

```bash
tidas-bw preflight tiangong-open-data.zip \
  --database tiangong-open-2026.08 \
  --strict-references
```

导入 Brightway：

```bash
tidas-bw import tiangong-open-data.zip \
  --project tiangong-open \
  --database tiangong-open-2026.08 \
  --biosphere-database tiangong-open-biosphere-2026.08 \
  --strict-references
```

若不写 `--biosphere-database`，环境流数据库默认为 `<database>-biosphere`。

将由本工具导入且未发生变化的数据库恢复为 TIDAS：

```bash
tidas-bw export \
  --project tiangong-open \
  --database tiangong-open-2026.08 \
  --output tiangong-open-2026.08.tidas.zip \
  --strict-references
```

从原生 Brightway 数据库生成新的 TIDAS 数据包时，必须说明许可、权属和来源：

```bash
tidas-bw export \
  --project my-project \
  --database my-open-database \
  --output my-open-database.tidas.zip \
  --license "CC BY 4.0" \
  --owner "数据发布者或权利人" \
  --source "公开数据的引用或网址" \
  --strict-references
```

若该数据库引用了外部 biosphere 数据库，每个外部数据库也必须在 Brightway 数据库元数据中具有自己的 `license`、`owner` 和 `source`；前景数据库的许可不会被套用到外部数据。

所有命令都可加 `--json`，输出适合脚本读取的迁移报告。已有目标默认不会覆盖；只有明确使用 `import --replace` 或 `export --overwrite` 才会替换。

## 双向迁移的含义

### TIDAS → Brightway

工具会：

- 按文档中的 UUID 和精确版本识别数据，而不是依赖文件名；
- 解析“交换—流—流属性—单位组—参考单位”的完整关系；
- 以 `resultingAmount` 建立 Brightway 交换；
- 优先采用生命周期模型中的显式供应者连接，其次使用交换或产品流的供给地点；
- 将产品过程和环境流分别写入 technosphere 与 biosphere 数据库；
- 将 LCIA 方法映射为可由 Brightway 计算的方法；
- 把原始 TIDAS 文档与身份信息保存在 Brightway 元数据中，以便核验后恢复；
- 在写入前检查 TIDAS Schema、开放许可、引用闭包、单位和供应者关系。

含变量的交换会以已经求值的 `resultingAmount` 进入 Brightway，并给出“参数化已冻结”的提示；原公式仍保存在原始 TIDAS 元数据中。

**数值精度**：默认保留严格的十进制往返检查，即原值须满足 `Decimal(str(float(原值))) == Decimal(原值)`。这允许 `0.01` 等普通小数，不代表二进制浮点能精确表示所有十进制数，更不代表 LCI/LCIA 结果没有计算误差。高精度小数若不满足该检查，会在写库前停止。

如需接受近似，必须显式指定 `--allow-rounding` 和适合数据用途的容差。例如，下例中的容差仅作演示，并非通用验收标准：

```bash
tidas-bw preflight data.zip --database example \
  --allow-rounding --absolute-tolerance 0 --relative-tolerance 1e-15
```

`import` 使用同样的选项。Python API 对应 `allow_rounding=True, absolute_tolerance="0", relative_tolerance="1e-15"`。绝对和相对容差默认均为零；允许误差为 `绝对容差 + 相对容差 × |原值|`，零附近由绝对容差控制。显式近似模式对每个交换量、表征因子及分布上下界检查实际 float64 表示误差，超限停止。报告的 `metadata.numeric_conversions` 逐条记录原值、目标值、精确二进制值、误差和容差；高精度舍入同时给出 warning。非有限数、溢出和下溢为零始终拒绝。

导入使用显式 float64 数组保存计算系数及不确定性参数，避免 bw-processing 1.6 默认迭代器中的 float32 中间转换。测试同时读取 Brightway 保存值和实际计算矩阵，并比较独立解析 LCI/LCIA 答案。原始文档始终另行保留；恢复文档不等于计算无损。此保证针对本工具完成的导入；随后直接调用 Brightway 自身的 `process()` 或修改数据会重新使用其处理流程。计算结果容差仍须针对具体模型另行验收，输入转换容差不是结果误差上限。

**不确定性**：支持独立的正态、正值对数正态和均匀分布，适用于交换及 LCIA 因子；Brightway 编号分别为 3、2、4，正态标准差写入 `scale`。公式、来源、单位和不支持范围见 [迁移约定](docs/migration-contract.md)。三种分布均通过实际 Brightway 随机 LCI/LCIA 计算、参数往返和修改检测。三角分布、缺失或矛盾参数、无法表示的数值范围、声明的相关性、带变量乘数的不确定交换、区域化交换和区域化表征因子继续拒绝。只有明确的零方差声明才可按确定性处理，溢出或下溢不能作为零方差。

### Brightway → TIDAS

分为两种情况：

1. **由 `tidas-bw` 导入的数据**：核对过程、流、交换、地点、数值、连接、方法和关键元数据均未改变后，恢复原始 TIDAS 文档。
2. **原生 Brightway 数据**：在具备明确开放许可、权属和来源的前提下，为过程、产品流、环境流、单位和流属性生成内容相关的稳定 UUID，并生成新的 TIDAS 数据包。

第一种情况是“无变化往返”，不是替代 TIDAS 版本管理。任何数量、地点、连接、名称、时间、分类、不确定性参数或 LCIA 因子的变化都会停止导出（Brightway 侧交换的不确定性会与保留的 TIDAS 声明逐字段比较），因为沿用旧 UUID 和版本号会造成错误身份。若确实要把 Brightway 中的修改发布回 TIDAS，应另行设计“创建新 TIDAS 版本”的工作流。

第二种情况只迁移有明确依据的内容，不虚构原始年份、分类、评审状态或数据来源。缺少必要元数据、多参考产品、外部 technosphere 供应者、参数公式、原生数据交换上的不确定性（由 TIDAS 导入的不确定性已可往返核对）或无法确定方向的环境流都会使导出停止。原生 Brightway 的 LCIA 方法目前不自动合成为新的 TIDAS 方法。

## 开放数据边界

- 过程、流、来源、LCIA 方法、生命周期模型在官方支持的字段中逐条证明开放许可。联系人、流属性、单位组使用根目录 `manifest.json` 的 `license_evidence`，逐条列出记录身份、许可证、权利人、来源及文档摘要；不继承主过程许可。缺证据、摘要不匹配或限制性许可都会停止。该清单与官方 tidas 0.2.1 完整校验兼容；官方格式校验本身不验证许可权属。格式和填写示例见 [迁移约定](docs/migration-contract.md)；
- `None` 或“未写限制”不等于授予开放许可；
- CC BY-NC、CC BY-ND、禁止商业使用、禁止再分发、仅内部研究、专有或收费许可会被拒绝；
- 许可声明与使用限制会分别审查，开放许可后附带的限制条款不能被忽略；
- 数据校验失败时不会创建或修改 Brightway 数据库；导出校验失败时不会写出 TIDAS 包。

## 安全和可重复性

- 替换 Brightway 数据库前会检查其他数据库的依赖；存在外部依赖时拒绝替换；
- 写入中途失败会恢复原数据库和相关 LCIA 方法；
- ZIP 会检查路径穿越、符号链接、重复身份、文件数和单文件大小；
- ZIP 输出采用确定性的文件顺序和时间戳，便于复核；
- 可用 `--brightway-dir /path/to/isolated/brightway-data` 将测试和正式项目隔离。

## 当前适用范围

当前版本面向有且只有一个正值参考产品的单位过程或已分配过程。主要目标是可靠迁移开放的 TIDAS 数据包，并支持未经修改的往返恢复；不是任意 Brightway 模型到完整 TIDAS 语义的自动翻译器。

项目适合作为独立包先行开发和验证，未来可作为 TIDAS 工具集中的可选模块，与 `tidas-ilcd` 放在同一转换工具层，但不应让 TIDAS 核心数据模型直接依赖 Brightway。

## Python API

```python
from tidas_bw import export_tidas, import_tidas, preflight_import, validate_tidas

report = validate_tidas("tiangong-open-data.zip", strict_references=True)

report = preflight_import(
    "tiangong-open-data.zip",
    database="tiangong-open-2026.08",
    strict_references=True,
)

report = import_tidas(
    "tiangong-open-data.zip",
    project="tiangong-open",
    database="tiangong-open-2026.08",
    strict_references=True,
)

report = export_tidas(
    project="my-project",
    database="my-open-database",
    output="my-open-database.tidas.zip",
    license="CC BY 4.0",
    owner="数据发布者或权利人",
    source="公开数据的引用或网址",
    strict_references=True,
)
```

## 开发验证

```bash
uv run ruff check .
uv run pytest
uv build
```

集成测试使用临时 Brightway 数据目录，不应接触已有项目。
