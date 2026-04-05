import subprocess
import time
import re
import json

def extract_with_kedou(url: str) -> dict:
    """
    使用 Kedou 视频解析站 + agent-browser 自动化提取微信公众号文章内容。
    支持图文完整还原。
    """
    KEDOU_URL = "https://www.kedou.life/extract/gzh"
    result = {
        "title": "",
        "content": "",
        "images": [],
        "source": "kedou_automation"
    }

    try:
        # 1. 打开 Kedou 页面
        subprocess.run(["agent-browser", "open", KEDOU_URL], check=True, capture_output=True)
        time.sleep(3)

        # 2. 填入链接并点击“开始”
        subprocess.run(["agent-browser", "fill", "input, textarea", url], check=True, capture_output=True)
        subprocess.run(["agent-browser", "click", "button:has-text('开始')"], check=True, capture_output=True)
        
        # 3. 动态轮询等待解析完成（监听进度条）
        print(f"[Kedou] Starting parsing for: {url}")
        max_retries = 20
        for i in range(max_retries):
            time.sleep(3)
            check = subprocess.run(
                ["agent-browser", "eval", "document.querySelector('.el-progress__text')?.innerText"], 
                capture_output=True, text=True
            )
            if "100%" in check.stdout:
                print("[Kedou] Parsing completed (100%).")
                break
            if i == max_retries - 1:
                raise Exception("Kedou parsing timeout.")

        # 4. 点击“Markdown”并“点击查看”
        subprocess.run(["agent-browser", "click", "button:has-text('markdown文本')"], check=True, capture_output=True)
        time.sleep(2)
        subprocess.run(["agent-browser", "click", "button:has-text('点击查看')"], check=True, capture_output=True)
        time.sleep(3)

        # 5. 提取弹窗内的 Markdown 文本
        text_extract = subprocess.run(
            ["agent-browser", "eval", "document.querySelector('.el-dialog__body')?.innerText"], 
            capture_output=True, text=True
        )
        result["content"] = text_extract.stdout if text_extract.stdout else "Extract failed."

        # 6. 提取所有图片直链
        img_extract = subprocess.run(
            ["agent-browser", "eval", "Array.from(document.querySelectorAll('img')).map(i => i.src).filter(s => s.includes('mmbiz'))"], 
            capture_output=True, text=True
        )
        try:
            result["images"] = json.loads(img_extract.stdout) if img_extract.stdout else []
        except:
            result["images"] = []

        # 7. 提取标题
        title_extract = subprocess.run(
            ["agent-browser", "eval", "document.querySelector('.js_title_inner')?.innerText"], 
            capture_output=True, text=True
        )
        result["title"] = title_extract.stdout if title_extract.stdout else "Unknown Title"

        subprocess.run(["agent-browser", "close"], check=True, capture_output=True)
        return result

    except Exception as e:
        print(f"[Kedou] Error: {str(e)}")
        try:
            subprocess.run(["agent-browser", "close"], check=True, capture_output=True)
        except:
            pass
        return result
