import sys
import os
sys.path.append(os.path.dirname(__file__))

from kedou_parser import extract_with_kedou
# 后续可以导入其他解析器 (jina_parser, changfeng_parser 等)

def auto_parse(url: str):
    """
    智能解析调度中心。
    采用四级降级策略：
    1. 直连抓取 (web_fetch)
    2. 长风盒子
    3. Jina AI
    4. Kedou 自动化 (终极兜底)
    """
    print(f"[Parser] Starting auto parse for: {url}")
    
    # 暂时先默认走 Kedou 链路进行演示整合
    # TODO: 后续加入 web_fetch 前置判断
    if "mp.weixin.qq.com" in url:
        print("[Parser] WeChat URL detected. Switching to Kedou engine...")
        return extract_with_kedou(url)
    
    return {"error": "Not implemented for this URL type yet."}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        result = auto_parse(sys.argv[1])
        print(f"Title: {result.get('title')}")
        print(f"Content Length: {len(result.get('content', ''))}")
        print(f"Images Found: {len(result.get('images', []))}")
