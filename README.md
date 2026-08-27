# aistudio-to-pdf

一键把 [Google AI Studio](https://aistudio.google.com) 的对话导出为排版精美的 PDF。

把对话链接丢给脚本，它会自动打开浏览器抓取完整对话（用户提问、AI 回复、AI 思考过程），渲染成 A4 排版的 PDF，方便存档、打印和分享。

## 功能特性

- **完整抓取，不漏消息** — 直接读取页面加载对话时的数据接口，拿到原始 Markdown 全文，比手动复制粘贴更完整（页面折叠的长回复、思考过程都不会丢）
- **自动区分角色** — 用户提问 / AI 回复 / AI 思考过程（thinking）分别用不同颜色的标签标注
- **自动生成封面** — 对话标题、所用模型、轮数统计、原始链接、导出时间
- **完整 Markdown 渲染** — 代码块、表格、列表、引用、标题层级都正常排版
- **多账号支持** — 对话在哪个 Google 账号下，就在弹出的浏览器里切换到哪个账号，脚本自动继续
- **批量导出** — 一条命令传多个链接，依次导出多个 PDF

## 环境要求

- Python 3.9+
- Microsoft Edge 或 Google Chrome（Windows / macOS）
- Linux：可用 Chromium（先运行 `python -m playwright install chromium`）
- 需要有图形界面（脚本会弹出浏览器窗口完成登录/账号切换，纯命令行服务器无法使用）

## 安装

```bash
git clone https://github.com/yongchangxu/aistudio_to_pdf.git
cd aistudio_to_pdf
pip install -r requirements.txt
```

或手动安装依赖：

```bash
pip install markdown playwright
```

## 使用

### 基本用法

```bash
python aistudio_to_pdf.py https://aistudio.google.com/prompts/xxxxxxxx
```

PDF 会以对话标题命名，保存在当前目录（同时生成一个同名 `.html` 排版预览文件，不需要可以删掉）。

### 首次运行（需要登录一次）

首次运行会弹出一个浏览器窗口：

1. 在窗口里登录你的 Google 账号
2. 登录完成后脚本自动继续，全程无需其他操作

登录状态保存在 `~/.aistudio_profile`（用户主目录，不在本仓库内），之后运行不再需要登录。

### 多账号切换

如果对话属于另一个账号（链接里的 `/u/1`、`/u/2` 就代表第 2、3 个账号），脚本会提示：

> 页面已打开但未捕获到对话数据，当前登录的账号可能不是这个对话的主人。

此时在浏览器窗口右上角点击账号头像，**切换到对话所属的账号**；如果列表里没有，选「添加其他账号」登录一次。切换后脚本自动继续。每个账号只需添加一次，之后所有该账号的对话都能直接导出。

### 批量导出

```bash
python aistudio_to_pdf.py 链接1 链接2 链接3
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --out 文件名.pdf` | 指定输出 PDF 路径（仅单个链接时可用） |
| `--no-thinking` | 导出的 PDF 不包含 AI 思考过程 |
| `--timeout 秒数` | 等待登录/加载的超时时间，默认 300 秒 |
| `--channel msedge/chrome/chromium` | 指定浏览器，默认 msedge，自动回退 |
| `--profile 目录` | 浏览器登录状态保存目录，默认 `~/.aistudio_profile` |
| `--keep-raw` | 额外保存抓到的原始 JSON 数据（调试用） |

### 示例

```bash
# 不包含思考过程，指定输出文件名
python aistudio_to_pdf.py https://aistudio.google.com/prompts/xxx -o "BIOS学习笔记.pdf" --no-thinking
```

## 工作原理

1. 用 Playwright 启动带界面的 Edge/Chrome，打开对话链接
2. 监听网络请求，捕获 AI Studio 加载对话的内部接口响应（含完整对话数据的 JSON）
3. 从响应中解析出所有轮次，并区分用户提问、AI 回复、思考过程
4. 把 Markdown 渲染成排版 HTML
5. 用无头浏览器将 HTML 打印为 A4 PDF

## 常见问题

**Q: 提示「未能捕获对话数据」？**
可能是：未在超时时间内完成登录；当前账号无权访问该对话；链接不是对话页面。按提示登录/切换账号后重试。

**Q: 弹出的浏览器窗口能关吗？**
不能。脚本需要这个窗口来完成登录、切换账号和抓取数据，抓取完成后会自动关闭。意外关闭的话脚本会自动重启重试一次。

**Q: 登录状态会过期吗？**
Google 登录一般可保持很久；如果哪天又提示登录，照常在弹出的窗口里登录一次即可。

**Q: 对话里的图片能导出吗？**
目前只导出文字内容（含代码块和表格），图片暂不支持。

**Q: 会不会把我的对话上传到服务器？**
不会。整个流程都在你本机的浏览器里完成，不经过任何第三方服务器。

## 隐私与安全须知

- `~/.aistudio_profile` 保存着你的 Google 登录凭据（cookie），**不要拷贝、分享或提交到任何仓库**（本仓库的 `.gitignore` 已排除同名目录，以防有人用 `--profile` 指到仓库内）
- 导出的 PDF/HTML 是你的私人对话内容，同样注意不要误提交（`.gitignore` 已默认排除 `*.pdf`、`*.html`）

## 许可证

[MIT](LICENSE)
