import os
import subprocess
import time
from typing import List, Dict, Optional
import anyio
from mcp.client.stdio import StdioServerParameters
from crewai_tools import MCPServerAdapter
from .knowledge_tools import Mem0Tool, MementoTool
from ..utils.logger import write_log_message, handle_application_error


# =============================================================================
# SafeToolLoader
# 설명: 로컬/외부 MCP 도구들을 안전하게 초기화·로드·종료 관리
# =============================================================================
class SafeToolLoader:
	"""도구 로더 클래스"""
	adapters = []
	
	ANYIO_PATCHED: bool = False

	def __init__(self, tenant_id: Optional[str] = None, user_id: Optional[str] = None, agent_name: Optional[str] = None, mcp_config: Optional[Dict] = None):
		"""실행 컨텍스트(tenant/user/agent)와 MCP 설정을 보관한다."""
		self.tenant_id = tenant_id
		self.user_id = user_id
		self.agent_name = agent_name
		self._mcp_servers = (mcp_config or {}).get('mcpServers', {})
		self.local_tools = ["mem0", "memento", "human_asked"]
		write_log_message(f"SafeToolLoader 초기화 완료 (tenant_id: {tenant_id}, user_id: {user_id})")

	# =============================================================================
	# Warmup (npx 서버 사전 준비)
	# =============================================================================
	def warmup_server(self, server_key: str, mcp_config: Optional[Dict] = None):
		"""npx 서버 패키지를 미리 캐싱해 최초 실행 지연을 줄인다."""
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

	# =============================================================================
	# 유틸: npx 경로 탐색
	# =============================================================================
	def _find_npx_command(self) -> str:
		"""npx 실행 파일 경로를 탐색해 반환한다."""
		try:
			import shutil
			npx_path = shutil.which("npx") or shutil.which("npx.cmd")
			if npx_path:
				return npx_path
		except Exception:
			pass
		return "npx"

	# =============================================================================
	# 로컬 도구 생성
	# =============================================================================
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

	# =============================================================================
	# 로컬 도구 로더들
	# =============================================================================
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
			return []
		except Exception as error:
			handle_application_error("툴human오류", error, raise_error=False)
			return []

	# =============================================================================
	# 외부 MCP 도구 로더
	# =============================================================================
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

	# =============================================================================
	# anyio 서브프로세스 stderr 패치
	# =============================================================================
	def _apply_anyio_patch(self):
		"""stderr에 fileno 없음 대비: PIPE로 보정해 예외를 방지한다."""
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

	# =============================================================================
	# 종료 처리
	# =============================================================================
	@classmethod
	def shutdown_all_adapters(cls):
		"""모든 MCPServerAdapter 연결을 안전하게 종료한다."""
		for adapter in cls.adapters:
			try:
				adapter.stop()
			except Exception as error:
				handle_application_error("툴종료오류", error, raise_error=False)
		write_log_message("모든 MCPServerAdapter 연결 종료 완료")
		cls.adapters.clear()
