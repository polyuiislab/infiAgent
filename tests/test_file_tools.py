import pytest
import json
from pathlib import Path
from tool_server_lite.tools.file_tools import (
    FileReadTool,
    FileWriteTool,
    DirListTool,
    DirCreateTool,
    FileMoveTool,
    FileDeleteTool,
)

pytestmark = pytest.mark.unit

@pytest.fixture
def workspace(tmp_path):
    """Fixture to provide a temporary workspace path."""
    return str(tmp_path)

class TestFileReadTool:
    def test_read_single_file_success(self, workspace):
        file_path = Path(workspace) / "test.txt"
        file_path.write_text("Hello World", encoding="utf-8")
        
        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "test.txt"})
        
        assert result["status"] == "success"
        assert "Hello World" in result["output"]

    def test_read_single_file_not_found(self, workspace):
        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "nonexistent.txt"})
        
        assert result["status"] == "error"
        assert "File not found" in result["error"]

    def test_read_multiple_files_success(self, workspace):
        (Path(workspace) / "f1.txt").write_text("C1", encoding="utf-8")
        (Path(workspace) / "f2.txt").write_text("C2", encoding="utf-8")

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": ["f1.txt", "f2.txt"]})

        assert result["status"] == "success"
        assert "\"success_count\": 2" in result["output"]

    def test_read_empty_file(self, workspace):
        """测试读取空文件"""
        (Path(workspace) / "empty.txt").touch()

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "empty.txt"})

        assert result["status"] == "success"

    def test_read_binary_file_error(self, workspace):
        """测试读取二进制文件应返回错误"""
        binary_file = Path(workspace) / "test.png"
        binary_file.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00')

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "test.png"})

        assert result["status"] == "error"
        assert "binary" in result["error"].lower()

    def test_read_with_line_range(self, workspace):
        """测试按行范围读取"""
        content = "line1\nline2\nline3\nline4\nline5\n"
        (Path(workspace) / "lines.txt").write_text(content, encoding="utf-8")

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "lines.txt", "start_line": 2, "end_line": 4})

        assert result["status"] == "success"
        output = json.loads(result["output"])
        assert len(output) == 3
        assert output[0]["line"] == 2
        assert output[0]["content"] == "line2"

    def test_read_without_line_numbers(self, workspace):
        """测试不显示行号"""
        (Path(workspace) / "test.txt").write_text("content", encoding="utf-8")

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": "test.txt", "show_line_numbers": False})

        assert result["status"] == "success"
        assert result["output"] == "content"

    def test_read_missing_path_parameter(self, workspace):
        """测试缺少 path 参数"""
        tool = FileReadTool()
        result = tool.execute(workspace, {})

        assert result["status"] == "error"
        assert "path" in result["error"].lower()

    def test_read_with_file_path_alias(self, workspace):
        """测试使用 file_path 参数别名"""
        (Path(workspace) / "test.txt").write_text("alias test", encoding="utf-8")

        tool = FileReadTool()
        result = tool.execute(workspace, {"file_path": "test.txt"})

        assert result["status"] == "success"
        assert "alias test" in result["output"]

    def test_read_multiple_files_partial_error(self, workspace):
        """测试多文件读取部分失败"""
        (Path(workspace) / "exists.txt").write_text("ok", encoding="utf-8")

        tool = FileReadTool()
        result = tool.execute(workspace, {"path": ["exists.txt", "not_exists.txt"]})

        assert result["status"] == "success"
        output = json.loads(result["output"])
        assert output["success_count"] == 1
        assert output["error_count"] == 1

class TestFileWriteTool:
    def test_write_file_success(self, workspace):
        tool = FileWriteTool()
        result = tool.execute(workspace, {"path": "new.txt", "content": "New Content"})

        assert result["status"] == "success"
        assert (Path(workspace) / "new.txt").read_text(encoding="utf-8") == "New Content"

    def test_write_creates_parent_dirs(self, workspace):
        """测试写入时自动创建父目录"""
        tool = FileWriteTool()
        result = tool.execute(workspace, {"path": "a/b/c/deep.txt", "content": "deep"})

        assert result["status"] == "success"
        assert (Path(workspace) / "a/b/c/deep.txt").read_text(encoding="utf-8") == "deep"

    def test_write_reference_bib_forbidden(self, workspace):
        """测试禁止写入 reference.bib"""
        tool = FileWriteTool()
        result = tool.execute(workspace, {"path": "reference.bib", "content": "test"})

        assert result["status"] == "error"
        assert "reference.bib" in result["error"]

    def test_replace_lines_file_not_found(self, workspace):
        """测试行替换时文件不存在"""
        tool = FileWriteTool()
        result = tool.execute(workspace, {
            "path": "nonexistent.py",
            "start_line": 1,
            "end_line": 2,
            "content": "new content"
        })

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    def test_append_file_success(self, workspace):
        p = Path(workspace) / "log.txt"
        p.write_text("Line1\n", encoding="utf-8")
        
        tool = FileWriteTool()
        result = tool.execute(workspace, {"path": "log.txt", "content": "Line2", "mode": "append"})
        
        assert result["status"] == "success"
        assert p.read_text(encoding="utf-8") == "Line1\nLine2"

    def test_replace_lines_success(self, workspace):
        p = Path(workspace) / "code.py"
        p.write_text("lines = [\n    'one',\n    'two',\n    'three'\n]\n", encoding="utf-8")
        
        tool = FileWriteTool()
        # Replace lines 2-4 (0-indexed 1-3)
        # Original:
        # 1: lines = [
        # 2:     'one',
        # 3:     'two',
        # 4:     'three'
        # 5: ]
        
        result = tool.execute(workspace, {
            "path": "code.py", 
            "start_line": 2, 
            "end_line": 4, 
            "content": "    'updated'"
        })
        
        assert result["status"] == "success"
        expected = "lines = [\n    'updated'\n]\n"
        assert p.read_text(encoding="utf-8") == expected

class TestDirListTool:
    def test_list_dir_success(self, workspace):
        (Path(workspace) / "a.txt").touch()
        (Path(workspace) / "b_dir").mkdir()

        tool = DirListTool()
        result = tool.execute(workspace, {"path": "."})

        assert result["status"] == "success"
        assert "[file] a.txt" in result["output"]
        assert "[dir] b_dir" in result["output"]

    def test_list_dir_not_found(self, workspace):
        """测试目录不存在"""
        tool = DirListTool()
        result = tool.execute(workspace, {"path": "nonexistent_dir"})

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    def test_list_not_a_directory(self, workspace):
        """测试路径不是目录"""
        (Path(workspace) / "file.txt").touch()

        tool = DirListTool()
        result = tool.execute(workspace, {"path": "file.txt"})

        assert result["status"] == "error"
        assert "not a directory" in result["error"].lower()

    def test_list_empty_directory(self, workspace):
        """测试空目录"""
        (Path(workspace) / "empty_dir").mkdir()

        tool = DirListTool()
        result = tool.execute(workspace, {"path": "empty_dir"})

        assert result["status"] == "success"
        assert "empty" in result["output"].lower()

    def test_list_recursive_success(self, workspace):
        (Path(workspace) / "parent").mkdir()
        (Path(workspace) / "parent/child.txt").touch()
        
        tool = DirListTool()
        result = tool.execute(workspace, {"path": ".", "recursive": True})
        
        assert result["status"] == "success"
        assert "[dir] parent" in result["output"]
        assert "  [file] child.txt" in result["output"]

class TestDirCreateTool:
    def test_create_dir_success(self, workspace):
        tool = DirCreateTool()
        result = tool.execute(workspace, {"path": "new_folder/sub_folder"})
        
        assert result["status"] == "success"
        assert (Path(workspace) / "new_folder/sub_folder").is_dir()

class TestFileMoveTool:
    def test_move_file_success(self, workspace):
        src = Path(workspace) / "src.txt"
        src.touch()
        (Path(workspace) / "dest").mkdir()
        
        tool = FileMoveTool()
        result = tool.execute(workspace, {"source": ["src.txt"], "destination": "dest/"})
        
        assert result["status"] == "success"
        assert not src.exists()
        assert (Path(workspace) / "dest/src.txt").exists()

    def test_copy_file_success(self, workspace):
        src = Path(workspace) / "src.txt"
        src.touch()
        (Path(workspace) / "dest").mkdir()
        
        tool = FileMoveTool()
        result = tool.execute(workspace, {"source": ["src.txt"], "destination": "dest/", "copy": True})
        
        assert result["status"] == "success"
        assert src.exists()
        assert (Path(workspace) / "dest/src.txt").exists()

class TestFileDeleteTool:
    def test_delete_file_success(self, workspace):
        f = Path(workspace) / "todel.txt"
        f.touch()

        tool = FileDeleteTool()
        result = tool.execute(workspace, {"path": "todel.txt"})

        assert result["status"] == "success"
        assert not f.exists()

    def test_delete_dir_success(self, workspace):
        d = Path(workspace) / "deldir"
        d.mkdir()
        (d / "f.txt").touch()

        tool = FileDeleteTool()
        result = tool.execute(workspace, {"path": "deldir"})

        assert result["status"] == "success"
        assert not d.exists()

    def test_delete_not_found(self, workspace):
        """测试删除不存在的路径"""
        tool = FileDeleteTool()
        result = tool.execute(workspace, {"path": "nonexistent"})

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()
