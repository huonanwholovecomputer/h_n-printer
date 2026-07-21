import os
import sys
from pathlib import Path

def merge_text_files(directory: str, output_name: str = "代码文件内容汇总.txt") -> None:
    """
    将 directory 目录下的指定子文件夹（h_n_print、printer_client、printer-backend）
    及其所有子目录中的指定类型文件合并到 output_name。
    每个文件内容前插入 “===== 文件名（相对路径）=====” 分隔行。
    自动排除输出文件自身和指定的脚本文件。
    """
    dir_path = Path(directory).resolve()
    if not dir_path.is_dir():
        print(f"错误：'{directory}' 不是一个有效的目录。")
        sys.exit(1)

    output_path = dir_path / output_name
    script_name = "汇总文件内容.py"  # 要排除的脚本文件名

    # 定义允许的文件扩展名（新增 .wxml 和 .wxss）
    allowed_extensions = {'.py', '.txt', '.js', '.json', '.c', '.cpp', '.h', '.wxml', '.wxss'}
    
    # 定义允许扫描的顶层子目录（仅处理这三个文件夹）
    allowed_subdirs = {'h_n_print', 'printer_client', 'printer-backend'}

    # 收集符合条件的文件
    files = []
    for f in dir_path.rglob("*"):
        if not f.is_file():
            continue
        # 排除输出文件和脚本自身
        if f.name == output_name or f.name == script_name:
            continue
        # 只处理指定扩展名
        if f.suffix.lower() not in allowed_extensions:
            continue

        # 获取相对于根目录的路径，检查其第一级目录是否在允许列表中
        rel_path = f.relative_to(dir_path)
        # 如果文件就在根目录下（即 parts 只有一层），则跳过，因为我们只处理指定子目录内的文件
        if len(rel_path.parts) < 2:
            continue
        first_dir = rel_path.parts[0]
        if first_dir in allowed_subdirs:
            files.append(f)

    if not files:
        print(f"在指定的子目录 {', '.join(allowed_subdirs)} 中没有找到指定类型（{', '.join(allowed_extensions)}）的文件。")
        return

    # 按文件路径排序，使输出更有条理
    files.sort(key=lambda x: str(x.relative_to(dir_path)))

    with output_path.open("w", encoding="utf-8") as outfile:
        outfile.write(f"===== 合并文件列表 (共 {len(files)} 个文件) =====\n")
        outfile.write(f"===== 扫描范围: {', '.join(allowed_subdirs)} 及其子目录 =====\n")
        outfile.write(f"===== 文件类型: {', '.join(allowed_extensions)} =====\n\n")
        
        for file_path in files:
            rel_path = file_path.relative_to(dir_path)
            separator = f"===== {rel_path} =====\n"
            outfile.write(separator)

            try:
                with file_path.open("r", encoding="utf-8") as infile:
                    content = infile.read()
                outfile.write(content)
                if not content.endswith("\n"):
                    outfile.write("\n")
            except UnicodeDecodeError:
                try:
                    with file_path.open("r", encoding="gbk", errors="ignore") as infile:
                        content = infile.read()
                    outfile.write(content)
                    if not content.endswith("\n"):
                        outfile.write("\n")
                except Exception as e:
                    outfile.write(f"[无法读取此文件: {e}]\n")
            except Exception as e:
                outfile.write(f"[处理文件时出错: {e}]\n")

            outfile.write("\n")

    print(f"已将所有文件内容合并到: {output_path}")
    print(f"共处理了 {len(files)} 个文件")
    print(f"处理的文件类型: {', '.join(allowed_extensions)}")
    print(f"扫描的子目录: {', '.join(allowed_subdirs)}")

if __name__ == "__main__":
    # 如果命令行提供了目录参数，则使用该目录，否则使用当前目录
    if len(sys.argv) > 1:
        target_dir = sys.argv[1]
    else:
        target_dir = "."

    merge_text_files(target_dir)
