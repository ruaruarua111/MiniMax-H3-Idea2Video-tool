# 上游来源

- 仓库：https://github.com/MiniMax-AI/MiniMax-H3
- 目录：https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
- 同步日期：2026-08-10（Asia/Shanghai）
- 上游提交：`05d91ff89f58b665e56424fd66db9ef0351b3015`

下列文件从上游原样复制，未做任何改写：

| 文件 | SHA-256 |
| --- | --- |
| `SKILL.md` | `AFB3CE1AC8C768643E98FD0A6CE8E1B0F79EBC34B2C9CBA678A48C0B0854C1C3` |
| `references/base-en.txt` | `088FB632FA3CBB18C9821024FB689E6F0F356F824C8F2E6B8E43A33239B137C7` |
| `references/ref-en.txt` | `B621067B41988C4D92BE715B7A98BA48338F4076AB6041D95FCF463EA7C8CCDD` |

单段工作台的 T2VA、I2VA、FL2VA 始终读取 `base-en.txt`，不会把三种基础模式改写成 Ref2VA 六段式。长视频工作台仅在用户固定人物参考图或系统已锁定人物参考帧、并通过 Hybrid 条件节点实际连接这些参考图时读取 `ref-en.txt`；没有固定参考图时仍使用基础三字段格式。
