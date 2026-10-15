# 阶段7：素材上传与内容发布

2026-10-14：本阶段仍为规划，以下脚本名称和示例不是已实现功能。当前具体实施顺序见 [下一步计划-阶段7](../阶段12-文档与交付/下一步计划-阶段7.md)：先单视频上传、文件哈希和媒体扫描，再编辑预览/草稿，最后明确素材与发布范围后做一条正式发布及作品回查。优先使用单台Control API的 `file/import`，字段为 `dir_path/file/scan`，并复用阶段6的账号身份与UI控制。尚未执行公开发布。

## 📋 任务目标

实现素材批量上传和 TikTok 内容自动发布。

## ✅ 任务清单

### 7.1 批量上传素材视频
- [ ] 上传视频到云机存储
- [ ] 指定目标路径（/sdcard/DCIM/）
- [ ] 等待上传完成
- [ ] 验证文件完整性

### 7.2 自动化发布流程
- [ ] 点击发布按钮（+号）
- [ ] 选择视频素材
- [ ] 点击"下一步"
- [ ] 添加文字/标题
- [ ] 添加文案描述
- [ ] 选择 BGM
- [ ] 应用滤镜
- [ ] 点击"发布"

### 7.3 发布验证
- [ ] 截图确认发布成功
- [ ] 记录发布时间
- [ ] 保存发布状态

## 📁 核心文件

- `upload_materials.py` - 素材上传主脚本
- `publish_content.py` - 内容发布主脚本
- `material_manager.py` - 素材管理器
- `publish_workflow.py` - 发布工作流
- `publish_validator.py` - 发布验证器

## 🔧 API 接口

### 文件上传
- `POST /android_api/v1/upload_file_android_upload` - 批量上传文件
- `POST /android_api/v1/upload_file_from_url_batch` - 从URL上传

### 自动化控制
- `POST /android_api/v2/{db_id}/workflow` - 执行工作流
- `POST /android_api/v2/{db_id}/input/click` - 点击操作
- `POST /android_api/v2/{db_id}/input/text` - 文本输入
- `POST /android_api/v2/{db_id}/input/swipe` - 滑动操作
- `POST /android_api/v2/{db_id}/accessibility/node` - UI节点操作

### 辅助接口
- `GET /container_api/v1/screenshots/{db_id}` - 截图验证

## 📤 上传示例

### 批量上传视频文件
```python
with open('video.mp4', 'rb') as f:
    response = requests.post(
        f"{base_url}/android_api/v1/upload_file_android_upload",
        files={'file': f},
        data={
            'db_ids': 'EDGE001,EDGE002,EDGE003',
            'path': '/sdcard/DCIM/'
        }
    )
```

### 从URL批量上传
```python
response = requests.post(
    f"{base_url}/android_api/v1/upload_file_from_url_batch",
    json={
        'url': 'https://example.com/video.mp4',
        'db_ids': 'EDGE001,EDGE002,EDGE003',
        'path': '/sdcard/DCIM/'
    }
)
```

## 🤖 发布工作流示例

```json
{
  "actions": [
    {
      "path": "input/click",
      "params": {"x": 540, "y": 1800}
    },
    {
      "path": "base/sleep",
      "params": {"duration": 2000}
    },
    {
      "path": "input/click",
      "params": {"x": 200, "y": 800}
    },
    {
      "path": "input/click",
      "params": {"x": 900, "y": 1800}
    },
    {
      "path": "input/text",
      "params": {"text": "这是视频标题 #热门"}
    },
    {
      "path": "input/click",
      "params": {"x": 540, "y": 1000}
    },
    {
      "path": "input/text",
      "params": {"text": "这是视频描述文案..."}
    },
    {
      "path": "input/click",
      "params": {"x": 900, "y": 100}
    }
  ]
}
```

## 💡 文案管理

### 文案模板
```yaml
captions:
  - title: "标题1 #热门 #推荐"
    description: "这是一条有趣的视频..."
  - title: "标题2 #好物分享"
    description: "分享一个实用技巧..."
```

### 动态文案生成
```python
import random

def generate_caption():
    titles = ["标题1", "标题2", "标题3"]
    hashtags = ["#热门", "#推荐", "#好物分享"]
    
    title = random.choice(titles)
    tag = random.choice(hashtags)
    
    return f"{title} {tag}"
```

## 📊 素材管理

### 素材库结构
```
素材库/
├── 视频/
│   ├── video_001.mp4
│   ├── video_002.mp4
│   └── video_003.mp4
├── 音乐/
│   ├── bgm_001.mp3
│   └── bgm_002.mp3
└── 文案/
    └── captions.yaml
```

### 素材分配策略
- **轮询分配**: 按顺序分配素材
- **随机分配**: 随机选择素材
- **指定分配**: 为每个账号指定素材

## ⚠️ 注意事项

1. **素材格式**: 确保视频格式兼容（MP4推荐）
2. **文件大小**: 单个视频不超过 100MB
3. **上传路径**: 推荐 `/sdcard/DCIM/` 或 `/sdcard/Download/`
4. **发布频率**: 避免短时间内大量发布
5. **文案规范**: 避免敏感词、违规内容

## 📊 输出产物

- `upload_status.json` - 上传状态记录
- `publish_status.json` - 发布状态记录
- `publish_screenshots/` - 发布过程截图
- `publish_log.txt` - 发布日志
