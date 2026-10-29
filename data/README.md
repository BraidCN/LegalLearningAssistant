# 数据文件（不在仓库里）

法条库和向量索引体积太大（`articles.db` 104 MB、`embeddings.f16.npy` 162 MB），超过 GitHub 单文件 100 MB 的限制，因此**没有放进仓库**。

第一次使用前，任选一种方式准备 `data/db/`：

## 方式一：自己构建（推荐，数据最新）

```bat
:: 1) 初始化环境（只做一次）
一键部署.cmd

:: 2) 拉取 just-laws 数据并建库（约几分钟；需要联网）
构建数据.cmd

:: 3) 可选：生成向量索引（约 13 分钟，用于语义检索；不生成只走全文检索）
.venv\Scripts\python.exe build\embed.py
```

完成后 `data/db/` 里应有：

| 文件 | 说明 |
|---|---|
| `articles.db` | 法条库（laws / articles / articles_fts，约 104 MB） |
| `embeddings.f16.npy` | 向量索引（可选，约 162 MB） |
| `ids.npy` | 向量对应的条文 id（可选） |

## 方式二：使用打包好的数据

如果 Releases 里提供了 `lawagent-data-*.zip`，下载后把其中的 `articles.db`、`embeddings.f16.npy`、`ids.npy` 放进 `data/db/` 即可。

## 说明

- 数据快照来自 [ImCa0/just-laws](https://github.com/ImCa0/just-laws)，效力状态以[国家法律法规数据库](https://flk.npc.gov.cn/)为准。
- 重建法条库会重建 `articles.db`，但不会动 `data/docs`、`data/users` 里已归档的资料。
- 归档资料、错题本、账号库都写在 `data/` 下，全部属于本机，不会上传。
