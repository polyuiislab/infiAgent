import base64
from pathlib import Path

def main(img_path):
    print('hihi')
    img_path = Path(img_path)
    print(img_path)
    if True: # 读取并编码图片
        with open(img_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
        
        # 判断图片格式
        suffix = img_path.suffix.lower()
        mime_type_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        mime_type = mime_type_map.get(suffix, 'image/jpeg')
    print(image_data)


if __name__ == '__main__':
    # 使用脚本所在目录作为基准路径
    script_dir = Path(__file__).parent
    img_path = script_dir / 'current.png'
    main(img_path)