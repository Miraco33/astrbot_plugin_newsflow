# NewsFlow · AstrBot 每日简报

完整的 AstrBot 新闻插件：采集、AI 筛选去重、中文摘要、历史简报、Plugin Page 和定时图片推送。2.0 起业务核心与适配层统一维护在此仓库，无需外部 `/NewsFlow` 源码挂载。

## 安装与运行条件

- 已验证 AstrBot 4.28.1，当前声明支持 `>=4.28.1,<4.29`。
- 在 AstrBot 通过仓库 URL 安装：`https://github.com/Miraco33/astrbot_plugin_newsflow`。
- Python 依赖见根目录 `requirements.txt`。Chromium 系统运行库和中文字体需由部署环境提供，首次缺少浏览器时仍走本地 Playwright 安装路径。
- 当前 Oracle A1 派生镜像已提供运行环境；普通 AstrBot 镜像不保证可以直接渲染。不会调用远程 HTML 渲染器。
- DeepSeek 是生产验证的提供商；其他保留的兼容入口仍属实验性支持。

## 配置与命令

在插件配置中填写 `ai_api_key`，按需要设置 `ai_provider`、`ai_api_base`、`ai_model`、`http_proxy` 和 `cron_expression`。默认每天 6:00 运行。推送目标在插件控制台「推送」选择真实会话并保存。

- `/简报`：读取今日简报并本地渲染图片。
- `/简报 YYYY-MM-DD`：读取指定日期简报。
- `/简报 状态`：查看状态。
- `/简报 运行`：执行流水线。

摘要要求 40–60 字、最多 80 字（含标点），展示层保留 100 字兜底；模型超长或数字补全时仍可能截断。主题、来源、故事配额和发布事件记忆沿用原核心规则。

## 更新

1. 在 NewsFlow 控制台「系统」点击「准备更新」。
2. 等待显示「已暂停且任务已结束，可以到 AstrBot 插件页更新」。
3. 在 AstrBot 插件管理中更新 NewsFlow；首次迁移后需给现有插件绑定「仓库源」。
4. 更新成功后 NewsFlow 自动恢复运行，核对版本和定时任务。放弃更新则点击「取消更新并恢复运行」。

准备更新期间不接收新业务任务，定时触发也会跳过，因此应选择远离 6:00 的空闲窗口，避免长时间停在准备状态。当前 AstrBot 更新器先替换代码、后卸载插件，不能在采集进行中直接点更新。常规 Python/页面更新不需要重启整个 AstrBot；镜像、系统库和运行时依赖变更另行维护。

GitHub 推送不会自动更新正在运行的插件。仓库源使用默认分支（本仓库为 `main`），不自动选择最新 Release；只把验证通过的版本推送到默认分支。

## 源码和数据

```text
main.py                AstrBot 命令、页面 API、Cron
core/                  采集、筛选、存储、简报、邮件
bridge/                配置、流水线与后台任务收尾
rendering/             容器内 Playwright 渲染
pages/dashboard/       插件控制台
standalone/            显式独立运行入口，生产不启用
tests/                 核心及适配层测试
docs/                  迁移审计和来源追溯
```

运行数据库始终是 `StarTools.get_data_dir("astrbot_plugin_newsflow") / "news.db"`。HTML 存放在同目录 `output/`，图片在 `rendered_newsletters/`。插件配置由 AstrBot 保存，浏览器缓存继续使用容器的 `PLAYWRIGHT_BROWSERS_PATH`。更新替换代码目录，不能把数据库、密钥、会话、输出或 Git 开发工作树放进生产插件目录。

原核心历史在 [Shuyuxu211/NewsFlow](https://github.com/Shuyuxu211/NewsFlow)，迁入文件的原始 SHA-256 见 [migration-source.json](docs/migration-source.json)。后续业务源码只在本仓库维护，不双向复制。

## 测试与独立入口

从包含本插件目录的父目录运行（使用已有环境，不自动安装依赖）：

```text
python -m unittest discover -s astrbot_plugin_newsflow/tests -v
python -m astrbot_plugin_newsflow.standalone --help
```

核心测试可独立运行，AstrBot 生命周期集成测试需要实际 AstrBot 环境。独立入口所需额外依赖见 `standalone/requirements.txt`；仅在显式调用独立入口时读取工作目录的 `api_config.env`。Oracle A1 不启动第二套 Web 服务、数据库或调度器。

[2.0 迁移审计与操作说明](docs/migration-2.0.md)
