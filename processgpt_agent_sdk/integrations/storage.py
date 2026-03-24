import asyncio
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional


logger = logging.getLogger(__name__)


async def upload_file_to_bucket(
    file: BinaryIO, file_name: str, proc_inst_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    파일을 Supabase Storage 버킷에 업로드합니다.

    Note:
        파일은 "files" 버킷의 "uploads" 디렉토리에 UUID suffix를 붙여 저장됩니다.
    """
    from ..database import get_db_client

    def _upload_file() -> Dict[str, Any]:
        try:
            client = get_db_client()
            current_pos = file.tell()
            file.seek(0, 2)
            file_size = file.tell()
            file.seek(current_pos)

            file_path = Path(file_name)
            actual_file_name = f"{file_path.stem}_{str(uuid.uuid4())[:8]}{file_path.suffix}"
            final_storage_path = f"uploads/{actual_file_name}"

            detected_content_type, _ = mimetypes.guess_type(actual_file_name)
            if not detected_content_type:
                detected_content_type = "application/octet-stream"

            bucket_name = "files"
            logger.info("📤 업로드 중: %s (버킷: %s)", final_storage_path, bucket_name)

            storage_api = client.storage.from_(bucket_name)
            file.seek(0)

            storage_api.upload(
                path=final_storage_path,
                file=file,
                file_options={"content-type": detected_content_type},
            )

            public_url = None
            try:
                public_url = storage_api.get_public_url(final_storage_path)
            except Exception:
                pass

            logger.info("✅ 업로드 완료: %s", final_storage_path)
            result = {
                "success": True,
                "storage_path": final_storage_path,
                "file_name": actual_file_name,
                "content_type": detected_content_type,
                "size": file_size,
            }
            if public_url:
                result["public_url"] = public_url
            return result
        except Exception as e:
            error_msg = f"파일 업로드 실패: {str(e)}"
            logger.error("❌ %s", error_msg, exc_info=e)
            return {"success": False, "error": error_msg}

    return await asyncio.to_thread(_upload_file)


async def upload_files_to_bucket(
    files: List[Dict[str, Any]], proc_inst_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """여러 파일을 Supabase Storage 버킷에 업로드합니다."""
    if not files:
        logger.info("📤 업로드할 파일이 없습니다")
        return []

    logger.info("📤 파일 업로드 시작: %d개 파일", len(files))
    upload_tasks = [
        upload_file_to_bucket(
            file=file_info.get("file"),
            file_name=file_info.get("file_name"),
            proc_inst_id=file_info.get("proc_inst_id", proc_inst_id),
        )
        for file_info in files
    ]

    results = await asyncio.gather(*upload_tasks)
    success_count = sum(1 for r in results if r.get("success"))
    logger.info("📤 업로드 완료: 성공 %d/%d", success_count, len(files))
    return results
