# 迁移约定与验收范围

## 许可证据

TIDAS 官方 eILCD/XSD 不允许联系人、流属性、单位组使用一般的许可字段。
这些记录必须在包根目录 `manifest.json` 的 `license_evidence` 中有明确声明；
键为 `category:UUID@version`，值为：

```json
{
  "license": "CC BY 4.0",
  "owner": "真实的权利人或发布者",
  "source": "能核对该记录许可覆盖范围的来源或引用",
  "document_sha256": "该记录文档的规范化 SHA-256"
}
```

摘要由 `tidas_bw.utils.semantic_hash(record.document)` 产生；它对 JSON 键排序、
使用 UTF-8 和紧凑序列化后计算 SHA-256，与记录身份一起避免证据错配。
摘要不是权属证明或数字签名。上述声明须来自实际授权，不能通过自动套用主过程许可补齐。
`license_evidence` 帮助函数仅用于调用者确实有权声明许可的记录。

读取目录和 ZIP 时均保留清单。导入把证据随原始文档保存，未经修改的回导恢复同一证据。
增加第三方辅助记录、修改其文档或版本，都需要重新核对并提供对应证据。
即便有清单证据，记录本身声明的限制性许可或独占访问仍然拒绝。
过程、流、来源、LCIA 方法和生命周期模型仍使用其自身支持的字段，不由清单绕过检查。

原生 Brightway 导出时，新合成的联系人、单位和流属性来自本项目的 MIT 模板，
清单明确记录这一来源；外部环境流仍须有它自己数据库的许可、权利人和来源。
这不是把前景许可复制给第三方数据。

官方 tidas 0.2.1 会保留根目录 manifest.json，并对数据记录做 JSON、eILCD/XSD 和语义往返检查。
它不解释本工具的 license_evidence，不替代许可证据核查。
旧版没有证据清单的辅助记录不再默认放行；必须补充真实授权依据。

## 不确定性

来源：[TIDAS 规范](https://github.com/tiangong-lca/tidas-spec)中交换和表征因子的
`relativeStandardDeviation95In` 字段，以及
[stats_arrays 的参数定义](https://stats-arrays.readthedocs.io/en/latest/#mapping-parameter-array-columns-to-uncertainty-distributions)。
CI 固定使用官方 tidas 0.2.1 资源，并通过 uv.lock 固定 SDK 和计算库版本。

令 m 为交换的 resultingAmount 或因子的 meanValue，p 为百分数字段：

| TIDAS 分布 | Brightway 表示 | 约束 |
|---|---|---|
| normal | type=3，loc=m，scale=abs(m)×p/200 | p≥0；可负均值；不接受额外 min/max；m=0 且 p>0 无法确定绝对标准差，拒绝 |
| log-normal | type=2，loc=ln(m)，scale=ln(p/100)/2 | m>0，p≥100；依据规范中乘除 SDg² 的区间定义，m 是几何均值/中位数，不是算术期望 |
| uniform | type=4，minimum、maximum | 完整有限边界、m 在区间内，p 必须为空 |
| undefined/缺省 | 固定值 | 不能同时有不确定性参数 |
| triangular | 拒绝 | 规范未明确众数，不能作假设 |

正态 p=0、对数正态 p=100、均匀分布两端明确相等时为零方差，会记录确定性处理说明。
正态标准差、对数尺度或边界不可表示、区间在 float64 中塌缩时拒绝，不能降为固定值。

数值、标准差和边界使用流的参考流属性对应的参考单位；LCIA 因子使用方法和环境流的参考单位。
本工具不自动把 kg 改成 g 等其他单位，也不凭单位名称猜比例。
若上游改变单位，均值、正态标准差和均匀边界须按同一比例变化，
对数正态 loc 须加 ln(比例)，scale 不变。测试覆盖单位数量级变化和负值。

本轮只支持独立边际分布。带 referenceToVariable 的不确定交换，
或 meanAmount 与 resultingAmount 不等的不确定交换会拒绝，避免猜测变量乘数和共同变化。
显式 correlation/covariance 及对应矩阵扩展会拒绝；自由文本里的关系没有可计算解释，
不得将含未结构化相关性说明的包当作已验证联合模型。
原生 Brightway 不确定数据向新 TIDAS 文档的合成仍不支持；
本轮支持的是 TIDAS 导入、实际随机计算和未经改变的回导。

## 精度与验收

默认严格模式保持原先十进制字符串往返契约，允许普通小数，不声称二进制精确。
显式近似模式检查每个输入转换的实际二进制误差；绝对/相对容差均默认零，
没有通用非零阈值。完整原文保存、数据库保存值、计算系数和最终结果是不同层次。
输入误差限制不能替代具体模型的结果误差验收。

计算数据使用 float64 数组，测试核对保存值、实际矩阵和解析答案。
实际 Brightway 随机 LCI/LCIA 对三种分布分别运行固定种子的 2048 次抽样，
均值相对误差限 2.5%、标准差相对误差限 8%；这些只用于所测合成分布。
参数往返检查不把数值先四舍五入到 12 位；可表示的小幅修改也会阻止沿用旧身份导出。

包格式失败时不继续映射，预检报告分别记录 package_ok 与 mapping_ok；
映射跳过时后者为 null。预检不创建 Brightway 项目，也不检查目标项目现有数据的重名或替换依赖。

本轮自动验收覆盖合成包、原生 Brightway 导出、官方跨工具校验、
不确定性随机计算和失败回滚。真实平台包仍需提供许可明确且依赖完整的数据，
不能把合成 fixture 或人为补许可的诊断副本当作真实数据验收。

LCIA 的命名空间、schema 版本及 geography 元素在 SDK 0.2.14 与官方 eILCD 校验中的必填约定不同。
内部校验现已补上这些已复现的兼容性检查；不会替用户补写未知的地理范围。
CI 同时校验含三种分布及 LCIA 方法的完整合成包。
真实平台包验收按本次用户要求暂不在此执行。
