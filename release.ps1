$ErrorActionPreference = "Stop"

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [switch]$TestPyPI
)

Write-Host "Version -> $Version"

# 1) 버전 반영
(Get-Content pyproject.toml) |
    ForEach-Object { $_ -replace '^version\s*=\s*"[^"]+"', 'version = "' + $Version + '"' } |
    Set-Content pyproject.toml

# 2) 빌드 정리 및 생성
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }
Get-ChildItem -Path . -Filter *.egg-info | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

python -m pip install --upgrade build twine | Out-Null
python -m build

# 3) 메타 검증
python -m twine check dist/*

# 4) 업로드
if ($TestPyPI) {
    if (-not $env:TEST_PYPI_TOKEN) { throw "TEST_PYPI_TOKEN env가 필요합니다." }
    python -m twine upload --repository-url https://test.pypi.org/legacy/ -u __token__ -p $env:TEST_PYPI_TOKEN dist/*
} else {
    if (-not $env:PYPI_TOKEN) { throw "PYPI_TOKEN env가 필요합니다." }
    python -m twine upload -u __token__ -p $env:PYPI_TOKEN dist/*
}

Write-Host "Released $Version"


