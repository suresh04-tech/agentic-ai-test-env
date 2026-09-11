# The instance is handed the *working tree*, not a git ref, so `terraform apply`
# deploys exactly the code on disk with no commit-and-push round trip and no
# git credentials on the box.
#
# Note: files are added by content, so this packs text files. If you ever add
# binary assets to the repo, switch this to `source_dir` + `excludes`.

data "archive_file" "app" {
  type        = "zip"
  output_path = "${path.module}/.build/app.zip"

  source_dir = local.app_root
  excludes = [
    ".git",
    ".github",
    "infra",
    ".env",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "build",
    "dist",
    "secrets",
    "credentials",
    ".DS_Store"
  ]
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name_prefix}-artifacts-${random_id.bucket_suffix.hex}"

  # Single-command destroy: the bucket must empty itself.
  force_destroy = true

  tags = { Name = "${local.name_prefix}-artifacts" }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_object" "app" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "app/${data.archive_file.app.output_md5}.zip"
  source = data.archive_file.app.output_path
  etag   = data.archive_file.app.output_md5

  # Objects encrypted with the bucket default (AES256).
  tags = { Name = "${local.name_prefix}-app-bundle" }
}
