# Configure Terraform itself, including its version, providers, and state storage.
terraform {
  # Require Terraform 1.10 or newer.
  required_version = ">= 1.10"
  # Declare the provider plugins this configuration needs.
  required_providers {
    # Use the official AWS provider, allowing version 6.x updates.
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # Store this configuration’s Terraform state in S3; supply the bucket when initializing the backend.
  backend "s3" {
    # Save the state object at this path inside the state bucket.
    key          = "assistant-agent/prod.tfstate"
    # Look for the state bucket in US East (N. Virginia).
    region       = "us-east-1"
    # Enable server-side encryption for the stored Terraform state.
    encrypt      = true
    # Use an S3 lock file to prevent concurrent Terraform state updates.
    use_lockfile = true
  }
}
# Create and manage AWS resources in the US East (N. Virginia) region.
provider "aws" { region = "us-east-1" }
# Look up the available Availability Zones in the configured AWS region.
data "aws_availability_zones" "available" { state = "available" }
# Read AWS’s public SSM parameter for the latest Amazon Linux 2023 ARM64 AMI.
data "aws_ssm_parameter" "ami" { name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64" }
# Look up the AWS account identity used to run Terraform.
data "aws_caller_identity" "current" {}
# Define the project name used for resource names and tags.
variable "name" {
  # Require this input to be a string.
  type    = string
  # Use assistant-agent as the project name unless overridden.
  default = "assistant-agent"
}
# Define an optional email address for instance health alerts.
variable "alert_email" {
  # Require this input to be a string.
  type    = string
  # Disable email alert resources by default by leaving the address empty.
  default = ""
}
# Define reusable values computed from inputs and AWS lookups.
locals {
  # Choose the first available zone for the instance and its attached data volume.
  az   = data.aws_availability_zones.available.names[0]
  # Define the Project tag shared by tagged resources.
  tags = { Project = var.name }
}
# Create the application’s virtual private cloud (VPC).
resource "aws_vpc" "main" {
  # Reserve the private IPv4 range 10.42.0.0–10.42.255.255 for the VPC.
  cidr_block           = "10.42.0.0/16"
  # Enable DNS resolution through the VPC’s DNS service.
  enable_dns_support   = true
  # Enable AWS-provided DNS hostnames for eligible instances in this VPC.
  enable_dns_hostnames = true
  # Add the project name as the resource’s Name tag alongside the shared Project tag.
  tags                 = merge(local.tags, { Name = var.name })
}
# Create an internet gateway to connect the VPC to the internet.
resource "aws_internet_gateway" "main" {
  # Place this resource in the VPC defined above.
  vpc_id = aws_vpc.main.id
  # Apply the shared Project tag to this resource.
  tags   = local.tags
}
# Create the subnet used by the internet-facing application instance.
resource "aws_subnet" "public" {
  # Place this resource in the VPC defined above.
  vpc_id                  = aws_vpc.main.id
  # Reserve 10.42.1.0–10.42.1.255 for the public subnet.
  cidr_block              = "10.42.1.0/24"
  # Use the same Availability Zone selected for the application instance.
  availability_zone       = local.az
  # Do not automatically assign public IPv4 addresses to instances launched here.
  map_public_ip_on_launch = false
  # Add a readable public-subnet name alongside the shared Project tag.
  tags                    = merge(local.tags, { Name = "${var.name}-public" })
}
# Create private subnets without assigning them the public internet route table.
resource "aws_subnet" "private" {
  # Create two private subnets, with instance indexes 0 and 1.
  count             = 2
  # Place this resource in the VPC defined above.
  vpc_id            = aws_vpc.main.id
  # Split the VPC into /24 ranges and select 10.42.10.0/24 and 10.42.11.0/24.
  cidr_block        = cidrsubnet("10.42.0.0/16", 8, count.index + 10)
  # Place each private subnet in a different available zone.
  availability_zone = data.aws_availability_zones.available.names[count.index]
  # Tag each private subnet with its project and a numbered name.
  tags              = merge(local.tags, { Name = "${var.name}-private-${count.index + 1}" })
}
# Create a route table that gives the public subnet an internet route.
resource "aws_route_table" "public" {
  # Place this resource in the VPC defined above.
  vpc_id = aws_vpc.main.id
  # Define an outbound route in this route table.
  route {
    # Match all IPv4 destinations not covered by a more specific route.
    cidr_block = "0.0.0.0/0"
    # Send that traffic through the VPC’s internet gateway.
    gateway_id = aws_internet_gateway.main.id
  }
  # Apply the shared Project tag to this resource.
  tags = local.tags
}
# Associate the public subnet with its internet-enabled route table.
resource "aws_route_table_association" "public" {
  # Select the public subnet created above.
  subnet_id      = aws_subnet.public.id
  # Use the public route table created above.
  route_table_id = aws_route_table.public.id
}
# Create the instance firewall rules for web traffic and outbound connections.
resource "aws_security_group" "web" {
  # Give the security group a project-specific name.
  name   = "${var.name}-web"
  # Place this resource in the VPC defined above.
  vpc_id = aws_vpc.main.id
  # Define an inbound traffic rule.
  ingress {
    # Start the allowed port range at HTTP port 80.
    from_port   = 80
    # End the allowed port range at 80, allowing only HTTP on this rule.
    to_port     = 80
    # Apply this rule to TCP traffic.
    protocol    = "tcp"
    # Allow this rule’s traffic to or from any IPv4 address.
    cidr_blocks = ["0.0.0.0/0"]
  }
  # Define an inbound traffic rule.
  ingress {
    # Start the allowed port range at HTTPS port 443.
    from_port   = 443
    # End the allowed port range at 443, allowing only HTTPS on this rule.
    to_port     = 443
    # Apply this rule to TCP traffic.
    protocol    = "tcp"
    # Allow this rule’s traffic to or from any IPv4 address.
    cidr_blocks = ["0.0.0.0/0"]
  }
  # Define an outbound traffic rule.
  egress {
    # Set the lower port placeholder; the all-protocols setting below makes ports irrelevant.
    from_port   = 0
    # Set the upper port placeholder; the all-protocols setting below makes ports irrelevant.
    to_port     = 0
    # Allow every IP protocol for outbound traffic.
    protocol    = "-1"
    # Allow this rule’s traffic to or from any IPv4 address.
    cidr_blocks = ["0.0.0.0/0"]
  }
  # Apply the shared Project tag to this resource.
  tags = local.tags
}
# Create a private Elastic Container Registry repository for application images.
resource "aws_ecr_repository" "app" {
  # Use the project name as the ECR repository name.
  name = var.name
  # Enable image vulnerability scanning when an image is pushed to this repository.
  image_scanning_configuration { scan_on_push = true }
  # Encrypt stored container images with ECR’s AES-256 encryption.
  encryption_configuration { encryption_type = "AES256" }
  # Apply the shared Project tag to this resource.
  tags = local.tags
}
# Create a retention policy to remove older container images.
resource "aws_ecr_lifecycle_policy" "app" {
  # Apply the retention policy to the application’s ECR repository.
  repository = aws_ecr_repository.app.name
  # Encode a highest-priority rule that expires the oldest images when more than 20 remain, regardless of tags.
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "Retain 20 images", selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }, action = { type = "expire" } }] })
}
# Create the IAM role that grants AWS permissions to the EC2 instance.
resource "aws_iam_role" "instance" {
  # Give the instance role or profile a project-specific name.
  name               = "${var.name}-instance"
  # Encode a trust policy allowing the EC2 service to assume this role.
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
  # Apply the shared Project tag to this resource.
  tags               = local.tags
}
# Attach AWS’s managed Systems Manager permissions to the instance role.
resource "aws_iam_role_policy_attachment" "ssm" {
  # Use the IAM role defined for the application instance.
  role       = aws_iam_role.instance.name
  # Allow the instance to connect to Systems Manager for management and Session Manager access.
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
# Attach AWS’s managed read-only ECR permissions to the instance role.
resource "aws_iam_role_policy_attachment" "ecr" {
  # Use the IAM role defined for the application instance.
  role       = aws_iam_role.instance.name
  # Allow the instance to authenticate to ECR and read container images and repository metadata.
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}
# Add an inline policy for reading the application’s SSM parameters.
resource "aws_iam_role_policy" "parameters" {
  # Attach this inline policy to the instance role.
  role   = aws_iam_role.instance.id
  # Allow single and batch parameter reads under this project’s path in the current AWS account and region.
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["ssm:GetParameter", "ssm:GetParameters"], Resource = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/*" }] })
}
# Create the instance profile that lets EC2 use the IAM role.
resource "aws_iam_instance_profile" "app" {
  # Give the instance role or profile a project-specific name.
  name = "${var.name}-instance"
  # Use the IAM role defined for the application instance.
  role = aws_iam_role.instance.name
}
# Create a separate persistent EBS data volume for the application.
resource "aws_ebs_volume" "data" {
  # Use the same Availability Zone selected for the application instance.
  availability_zone = local.az
  # Allocate 30 GiB of storage for the data volume.
  size              = 30
  # Use gp3 general-purpose SSD storage.
  type              = "gp3"
  # Encrypt the volume at rest using the default EBS encryption key.
  encrypted         = true
  # Tag the data volume with its project and a readable name.
  tags              = merge(local.tags, { Name = "${var.name}-data" })
  # Reject Terraform plans that would destroy or replace this resource while this guard remains configured.
  lifecycle { prevent_destroy = true }
}
# Launch the EC2 instance that runs the application.
resource "aws_instance" "app" {
  # Boot from the Amazon Linux 2023 ARM64 AMI retrieved from SSM.
  ami                         = data.aws_ssm_parameter.ami.value
  # Use a burstable ARM-based t4g.medium instance with 2 vCPUs and 4 GiB of memory.
  instance_type               = "t4g.medium"
  # Select the public subnet created above.
  subnet_id                   = aws_subnet.public.id
  # Attach the web security group to control network traffic.
  vpc_security_group_ids      = [aws_security_group.web.id]
  # Give the instance the AWS permissions provided by its instance profile.
  iam_instance_profile        = aws_iam_instance_profile.app.name
  # Skip an automatically assigned public IPv4 address; an Elastic IP is associated below.
  associate_public_ip_address = false
  # Load the instance startup script from user-data.sh beside this Terraform file.
  user_data                   = file("${path.module}/user-data.sh")
  # Replace the instance if its startup script content changes.
  user_data_replace_on_change = true
  # Require IMDSv2 session tokens when accessing instance metadata.
  metadata_options { http_tokens = "required" }
  # Configure the instance’s boot volume.
  root_block_device {
    # Encrypt the volume at rest using the default EBS encryption key.
    encrypted   = true
    # Use gp3 general-purpose SSD storage for the boot volume.
    volume_type = "gp3"
  }
  # Enable detailed EC2 monitoring with one-minute CloudWatch metrics.
  monitoring = true
  # Add the project name as the resource’s Name tag alongside the shared Project tag.
  tags       = merge(local.tags, { Name = var.name })
}
# Attach the persistent data volume to the application instance.
resource "aws_volume_attachment" "data" {
  # Request this attachment device name; the guest OS may expose it as an NVMe device.
  device_name                    = "/dev/sdf"
  # Attach the persistent EBS volume created above.
  volume_id                      = aws_ebs_volume.data.id
  # Select the application instance.
  instance_id                    = aws_instance.app.id
  # Stop the instance before Terraform detaches this volume.
  stop_instance_before_detaching = true
}
# Allocate a stable public IPv4 address for the application.
resource "aws_eip" "app" {
  # Allocate the Elastic IP for use with VPC resources.
  domain = "vpc"
  # Apply the shared Project tag to this resource.
  tags   = local.tags
}
# Associate the stable Elastic IP with the application instance.
resource "aws_eip_association" "app" {
  # Select the application instance.
  instance_id   = aws_instance.app.id
  # Use the Elastic IP allocation created above.
  allocation_id = aws_eip.app.id
}
# Create an alarm for EC2 instance or system status-check failures.
resource "aws_cloudwatch_metric_alarm" "status" {
  # Give the status-check alarm a project-specific name.
  alarm_name          = "${var.name}-instance-status"
  # Consider a metric value above the threshold a breach.
  comparison_operator = "GreaterThanThreshold"
  # Require two consecutive breaching periods before entering the alarm state.
  evaluation_periods  = 2
  # Watch the metric that reports failed EC2 status checks.
  metric_name         = "StatusCheckFailed"
  # Read the metric from EC2’s CloudWatch namespace.
  namespace           = "AWS/EC2"
  # Evaluate the metric in 60-second intervals.
  period              = 60
  # Use the highest reported status-check value in each interval.
  statistic           = "Maximum"
  # Treat any nonzero status-check failure value as a breach.
  threshold           = 0
  # Limit the monitored metric to the application instance.
  dimensions          = { InstanceId = aws_instance.app.id }
  # Notify the SNS alerts topic on alarm only when an email address is configured.
  alarm_actions       = var.alert_email == "" ? [] : [aws_sns_topic.alerts[0].arn]
}
# Create an SNS topic for optional instance health notifications.
resource "aws_sns_topic" "alerts" {
  # Create this alert resource only when an email address is supplied.
  count = var.alert_email == "" ? 0 : 1
  # Give the notifications topic a project-specific name.
  name  = "${var.name}-alerts"
}
# Subscribe the configured email address to alerts; the recipient must confirm the subscription.
resource "aws_sns_topic_subscription" "email" {
  # Create this alert resource only when an email address is supplied.
  count     = var.alert_email == "" ? 0 : 1
  # Subscribe to the optional alerts topic created above.
  topic_arn = aws_sns_topic.alerts[0].arn
  # Deliver notifications by email.
  protocol  = "email"
  # Send the subscription confirmation and confirmed notifications to this email address.
  endpoint  = var.alert_email
}
# Expose the EC2 instance ID for management and deployment commands.
output "instance_id" { value = aws_instance.app.id }
# Expose the stable public IPv4 address for DNS and application access.
output "elastic_ip" { value = aws_eip.app.public_ip }
# Expose the ECR repository URL for tagging and pushing container images.
output "ecr_repository_url" { value = aws_ecr_repository.app.repository_url }
# Expose the persistent EBS volume ID for storage management.
output "data_volume_id" { value = aws_ebs_volume.data.id }
