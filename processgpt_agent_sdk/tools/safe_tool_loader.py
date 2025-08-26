import os
import subprocess
import time
from typing import List, Dict, Optional
import anyio
from mcp.client.stdio import StdioServerParameters
from crewai_tools import MCPServerAdapter
from .knowledge_tools import Mem0Tool, MementoTool
from ..utils.logger import write_log_message, handle_application_error


class SafeToolLoader:
	"""도구 로더 클래스"""
	adapters = []  # MCPServerAdapter 인스턴스 등록
	
	ANYIO_PATCHED: bool = False

	def __init__(self, tenant_id: Optional[str] = None, user_id: Optional[str] = None, agent_name: Optional[str] = None, mcp_config: Optional[Dict] = None):
		self.tenant_id = tenant_id
		self.user_id = user_id
		self.agent_name = agent_name
		# 외부에서 전달된 MCP 설정 사용 (DB 접근 금지)
		self._mcp_servers = (mcp_config or {}).get('mcpServers', {})
		self.local_tools = ["mem0", "memento", "human_asked"]
		write_log_message(f"SafeToolLoader 초기화 완료 (tenant_id: {tenant_id}, user_id: {user_id})")

	def warmup_server(self, server_key: str, mcp_config: Optional[Dict] = None):
		"""npx 기반 서버의 패키지를 미리 캐시에 저장해 실제 실행을 빠르게."""
		servers = (mcp_config or {}).get('mcpServers') or self._mcp_servers or {}
		server_config = servers.get(server_key, {}) if isinstance(servers, dict) else {}
		if not server_config or server_config.get("command") != "npx":
			return
			
		npx_command_path = self._find_npx_command()
		if not npx_command_path:
			return
			
		arguments_list = server_config.get("args", [])
		if not (arguments_list and arguments_list[0] == "-y"):
			return
			
		package_name = arguments_list[1]
		
		try:
			subprocess.run([npx_command_path, "-y", package_name, "--help"], capture_output=True, timeout=10, shell=True)
			return
		except subprocess.TimeoutExpired:
			pass
		except Exception:
			pass
			
		try:
			subprocess.run([npx_command_path, "-y", package_name, "--help"], capture_output=True, timeout=60, shell=True)
		except Exception:
			pass

	def _find_npx_command(self) -> str:
		"""npx 명령어 경로 찾기"""
		try:
			import shutil
			npx_path = shutil.which("npx") or shutil.which("npx.cmd")
			if npx_path:
				return npx_path
		except Exception:
			pass
		return "npx"

	def create_tools_from_names(self, tool_names: List[str], mcp_config: Optional[Dict] = None) -> List:
		"""tool_names 리스트에서 실제 Tool 객체들 생성"""
		if isinstance(tool_names, str):
			tool_names = [tool_names]
		write_log_message(f"도구 생성 요청: {tool_names}")
		
		tools = []
		
		tools.extend(self._load_mem0())
		tools.extend(self._load_memento())
		tools.extend(self._load_human_asked())
		
		for name in tool_names:
			key = name.strip().lower()
			if key in self.local_tools:
				continue
			else:
				self.warmup_server(key, mcp_config)
				tools.extend(self._load_mcp_tool(key, mcp_config))
		
		write_log_message(f"총 {len(tools)}개 도구 생성 완료")
		return tools

	def _load_mem0(self) -> List:
		"""mem0 도구 로드 - 에이전트별 메모리"""
		try:
			if not self.user_id:
				write_log_message("mem0 도구 로드 생략: user_id 없음")
				return []
			return [Mem0Tool(tenant_id=self.tenant_id, user_id=self.user_id)]
		except Exception as error:
			handle_application_error("툴mem0오류", error, raise_error=False)
			return []

	def _load_memento(self) -> List:
		"""memento 도구 로드"""
		try:
			return [MementoTool(tenant_id=self.tenant_id)]
		except Exception as error:
			handle_application_error("툴memento오류", error, raise_error=False)
			return []

	def _load_human_asked(self) -> List:
		"""human_asked 도구 로드 (선택사항: 사용 시 외부에서 주입)"""
		try:
			# 필요한 경우 외부에서 HumanQueryTool을 이 패키지에 추가하여 import하고 리턴하도록 변경 가능
			return []
		except Exception as error:
			handle_application_error("툴human오류", error, raise_error=False)
			return []

	def _load_mcp_tool(self, tool_name: str, mcp_config: Optional[Dict] = None) -> List:
		"""MCP 도구 로드 (timeout & retry 지원)"""
		self._apply_anyio_patch()
		
		servers = (mcp_config or {}).get('mcpServers') or self._mcp_servers or {}
		server_config = servers.get(tool_name, {}) if isinstance(servers, dict) else {}
		if not server_config:
			return []
		
		environment_variables = os.environ.copy()
		environment_variables.update(server_config.get("env", {}))
		timeout_seconds = server_config.get("timeout", 40)

		max_retries = 2
		retry_delay = 5

		for attempt in range(1, max_retries + 1):
			try:
				cmd = server_config["command"]
				if cmd == "npx":
					cmd = self._find_npx_command() or cmd
				
				params = StdioServerParameters(
					command=cmd,
					args=server_config.get("args", []),
					env=environment_variables,
					timeout=timeout_seconds
				)
				
				adapter = MCPServerAdapter(params)
				SafeToolLoader.adapters.append(adapter)
				write_log_message(f"{tool_name} MCP 로드 성공 (툴 {len(adapter.tools)}개): {[tool.name for tool in adapter.tools]}")
				return adapter.tools

			except Exception as e:
				if attempt < max_retries:
					time.sleep(retry_delay)
				else:
					handle_application_error(f"툴{tool_name}오류", e, raise_error=False)
					return []

	def _apply_anyio_patch(self):
		"""anyio stderr 패치 적용"""
		if SafeToolLoader.ANYIO_PATCHED:
			return
		from anyio._core._subprocesses import open_process as _orig

		async def patched_open_process(*args, **kwargs):
			stderr = kwargs.get('stderr')
			if not (hasattr(stderr, 'fileno') and stderr.fileno()):
				kwargs['stderr'] = subprocess.PIPE
			return await _orig(*args, **kwargs)

		anyio.open_process = patched_open_process
		anyio._core._subprocesses.open_process = patched_open_process
		SafeToolLoader.ANYIO_PATCHED = True

	@classmethod
	def shutdown_all_adapters(cls):
		"""모든 MCPServerAdapter 연결 종료"""
		for adapter in cls.adapters:
			try:
				adapter.stop()
			except Exception as error:
				handle_application_error("툴종료오류", error, raise_error=False)
		write_log_message("모든 MCPServerAdapter 연결 종료 완료")
		cls.adapters.clear()
