# Configure Terraform itself, including its version, providers, and state storage.
terraform {
  # Require Terraform 1.10 or newer.
  required_version = ">= 1.10"
  # Declare the provider plugins this configuration needs.
  required_providers {
    # Use the official AWS provider, allowing version 6.x updates.
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
# Create and manage AWS resources in the US East (N. Virginia) region.
provider "aws" { region = "us-east-1" }
# Require the caller to supply a globally unique S3 bucket name for Terraform state.
variable "state_bucket_name" { type = string }
# Create the S3 bucket that will hold the main configuration’s Terraform state.
resource "aws_s3_bucket" "state" {
  # Use the state bucket name supplied by the caller.
  bucket = var.state_bucket_name
  # Reject plans that destroy or replace this bucket while this guard remains configured.
  lifecycle { prevent_destroy = true }
}
# Configure versioning so previous state object versions can be recovered.
resource "aws_s3_bucket_versioning" "state" {
  # Apply this setting to the state bucket created above.
  bucket = aws_s3_bucket.state.id
  # Keep previous versions when state objects are overwritten or deleted.
  versioning_configuration { status = "Enabled" }
}
# Set the default encryption configuration for state objects in the bucket.
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  # Apply this setting to the state bucket created above.
  bucket = aws_s3_bucket.state.id
  # Define the bucket’s default encryption rule.
  rule {
    # Specify server-side encryption for newly stored objects.
    apply_server_side_encryption_by_default {
      # Use S3-managed encryption keys with AES-256 encryption.
      sse_algorithm = "AES256"
    }
  }
}
# Block public access to the Terraform state bucket.
resource "aws_s3_bucket_public_access_block" "state" {
  # Apply this setting to the state bucket created above.
  bucket                  = aws_s3_bucket.state.id
  # Reject new public bucket or object access-control lists (ACLs).
  block_public_acls       = true
  # Reject new bucket policies that grant public access.
  block_public_policy     = true
  # Ignore public permissions granted by existing bucket or object ACLs.
  ignore_public_acls      = true
  # Restrict access granted through public bucket policies to AWS services and authorized users in this account.
  restrict_public_buckets = true
}
# Expose the bucket name to use when initializing the main configuration’s S3 backend.
output "state_bucket_name" { value = aws_s3_bucket.state.id }
