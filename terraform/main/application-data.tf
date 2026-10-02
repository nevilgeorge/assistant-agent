# Name the S3 bucket used for manually uploaded Gmail and Calendar exports.
variable "application_data_bucket_name" {
  type = string
}

# Keep exported user data separate from Terraform state.
resource "aws_s3_bucket" "application_data" {
  bucket = var.application_data_bucket_name
  tags   = merge(local.tags, { Name = "${var.name}-data" })

  lifecycle { prevent_destroy = true }
}

# Retain previous versions to allow recovery from accidental overwrites or deletes.
resource "aws_s3_bucket_versioning" "application_data" {
  bucket = aws_s3_bucket.application_data.id
  versioning_configuration { status = "Enabled" }
}

# Encrypt newly uploaded objects with S3-managed keys.
resource "aws_s3_bucket_server_side_encryption_configuration" "application_data" {
  bucket = aws_s3_bucket.application_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Reject public bucket policies and ACL-based public access.
resource "aws_s3_bucket_public_access_block" "application_data" {
  bucket                  = aws_s3_bucket.application_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Require TLS for all requests, including bucket-level and object-level operations.
resource "aws_s3_bucket_policy" "application_data" {
  bucket = aws_s3_bucket.application_data.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.application_data.arn,
        "${aws_s3_bucket.application_data.arn}/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Print the destination for the manual export upload.
output "application_data_bucket_name" {
  value = aws_s3_bucket.application_data.id
}
