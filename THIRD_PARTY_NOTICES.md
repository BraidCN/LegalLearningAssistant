# 第三方项目与数据来源

本项目在设计与实现过程中参考、使用或分发了下列项目与数据。各自版权归原作者所有，许可如下。

## 代码与思路

| 项目 | 许可 | 用途 |
|---|---|---|
| [Golden2002/legal-research-skill](https://github.com/Golden2002/legal-research-skill) | MIT | “条号存在 + 引文逐字一致”的引用核验思路；本项目为独立实现，未复制其代码 |
| [fxsjy/jieba](https://github.com/fxsjy/jieba) | MIT | 中文分词（依赖，不随仓库分发） |
| [Qwen3](https://github.com/QwenLM/Qwen3) | Apache-2.0 | 本地生成模型（可选，依赖 Ollama 拉取，不随仓库分发） |

## 数据

| 来源 | 许可/状态 | 说明 |
|---|---|---|
| [ImCa0/just-laws](https://github.com/ImCa0/just-laws) | MIT | 法条正文基础数据 |
| [lawtext/laws](https://github.com/lawtext/laws) | 仓库未声明许可证；文本来自[国家法律法规数据库](https://flk.npc.gov.cn/) | 行政法规、司法解释与已废止文本。法律、法规等官方文件依《中华人民共和国著作权法》第五条不适用著作权法；本项目不随仓库分发其原始文件，仅提供抓取脚本 |
| 国家法律法规数据库 | 官方公开数据 | 效力状态与文本校对的最终依据 |

## 说明

- 若您是上述项目的作者并认为本项目的署名或许可标注有误，请开 Issue，我们会尽快更正。
- 本项目代码采用 MIT 许可（见 [LICENSE](LICENSE)），但上述第三方数据不因本项目许可而改变其原有许可状态。
