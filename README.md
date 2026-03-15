# WxReader · 微信读书自动阅读

> 模拟真实阅读行为，自动累积微信读书时长。支持 Web 界面、命令行、GitHub Actions 三种使用方式。

## 界面预览

<table>
  <tr>
    <td align="center"><b>配置</b></td>
    <td align="center"><b>状态</b></td>
    <td align="center"><b>历史</b></td>
  </tr>
  <tr>
    <td><img src="pictures/wxreader.yuanyinglight.com_(iPhone 14 Pro Max).png" width="220"/></td>
    <td><img src="pictures/wxreader.yuanyinglight.com_(iPhone 14 Pro Max) (1).png" width="220"/></td>
    <td><img src="pictures/wxreader.yuanyinglight.com_(iPhone 14 Pro Max) (2).png" width="220"/></td>
  </tr>
</table>

## 功能特性

- **三种登录方式**：纯 Cookie 字符串 / curl 命令 / 微信扫码登录
- **实时进度**：进度条 + 终端日志，阅读状态一目了然
- **历史记录**：每次任务的时长、状态持久保存
- **多用户隔离**：基于浏览器 client_id，多人共用互不干扰
- **自动续期**：Cookie 过期自动调用 renewal 接口刷新
- **移动端适配**：底部 Tab 导航，手机直接使用

## 快速开始

### 方式一：GitHub Actions（推荐，免费全自动）

1. Fork 本仓库
2. `Settings → Secrets → Actions → New secret`
   - Name: `WXREAD_COOKIE`
   - Value: `wr_skey=xxx; wr_vid=xxx; ...`
3. 每天 10:00 / 21:00（北京时间）自动运行，也可手动触发

### 方式二：Docker 部署

```bash
# 克隆仓库
git clone https://github.com/你的用户名/WxReader.git
cd WxReader

# 启动
docker compose up -d --build

# 访问
open http://localhost:39876
```

### 方式三：本地命令行

```bash
pip install -r requirements.txt

# 编辑配置
cp config.yaml.example config.yaml
# 填写 cookie 和目标时长

python main.py
```

### 方式四：Web 界面（本地）

```bash
pip install -r requirements.txt
python app.py
# 访问 http://localhost:8080
```

## 获取 Cookie

1. Chrome 打开 [weread.qq.com](https://weread.qq.com) 并登录
2. 按 `F12` 打开开发者工具 → Network
3. 刷新页面，随便点一个请求
4. 右键 → **Copy → Copy as cURL**
5. 粘贴到输入框，自动解析

> Cookie 有效期约数天，过期后重新获取或使用扫码登录。

## 项目结构

```
WxReader/
├── main.py              # 核心阅读逻辑
├── app.py               # Flask Web 服务
├── templates/
│   └── index.html       # 前端页面
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .github/workflows/
    └── read.yml         # GitHub Actions 定时任务
```

## 配置说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 目标时长 | 60 分钟 | 本次任务累积阅读时长 |
| 请求间隔 | 28-35 秒 | 每次请求随机间隔，模拟真实阅读 |

## License

MIT
