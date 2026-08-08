# 贡献指南（CONTRIBUTING）

感谢你愿意改进这个项目！下面是本地开发与提交流程。

## 一、本地开发环境

```bash
# 1) 安装依赖（Windows 双击 安装依赖.bat；macOS 跑 安装依赖.command）
# 2) 启动开发版（不打包，直接跑源码）
python app.py        # Windows
./启动.command       # macOS
```

## 二、改动前请确认

1. **不破坏四条硬约束**：绝不重编码 / 降画质 / 覆盖 / 篡改原文件。任何涉及 `shutil.move` / `rename` / 像素写入的改动都要格外小心。
2. **保持离线能力**：默认运行时不得引入联网调用。模型权重加载走本地缓存；如需新增联网功能，必须默认关闭且明确告知用户。
3. **跨平台兼容**：核心逻辑不得依赖 Windows 专有 API。路径处理用 `os.path` / `pathlib`；长路径问题在文档中提示，不硬编码 `\\?\`。

## 三、提交前必做

```bash
python selftest.py --no-ai     # 规则流程自检（快）
python selftest.py             # 含语义识别的完整自检（需模型已下载）
```

两条都应通过（含逐字节 MD5 一致、零覆盖、撤销还原）。

## 四、提交流程

1. Fork 本仓库到你的账号。
2. 从 `main`（或当前开发分支）切出功能分支：`git checkout -b fix/xxx` 或 `feat/xxx`。
3. 提交信息清晰：一句话说明「做了什么 + 为什么」。
4. 更新 `CHANGELOG.md`（在 `Unreleased` 段加条目），如有版本变更同步 `VERSION`。
5. 推到你的 Fork，向主仓库提 Pull Request，描述改动与测试结果。

## 五、代码风格

- Python 3.10+，遵循 PEP 8。
- 核心模块在 `core/`：`config`（配置）/ `media`（只读读取）/ `classifier`（分类引擎）/ `organizer`（命名归档撤销）。
- 新增类别无需改代码，编辑 `categories.json` 即可；只有引擎行为变化才动 `classifier.py`。
- 涉及数据安全的改动请在 PR 里重点说明测试方式。

## 六、接收外部修改（维护者）

1. 在 GitHub 上 Review PR，确认 `selftest.py` 通过、无新增联网/数据外发。
2. 合并进主仓库。
3. 本地 `git pull` 拉回更新，据此继续迭代下一个版本。
