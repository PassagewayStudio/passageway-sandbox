terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = [var.availability_zone]
  }
}

# Latest Canonical Ubuntu 24.04 LTS AMI
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

# No inbound rules at all. Users reach the host only through the Cloudflare Tunnel
# (outbound connection), and you reach it with SSM Session Manager.
resource "aws_security_group" "sandbox" {
  name        = "passageway-sandbox-host"
  description = "Sandbox host: no inbound, outbound only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "Outbound (tunnel, package installs, image pulls)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "passageway-sandbox-host" }
}

resource "aws_iam_role" "sandbox" {
  name = "passageway-sandbox-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

# Lets you open a shell with Session Manager (no SSH, no open port 22)
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.sandbox.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Lets the host read ONLY the tunnel token at boot
resource "aws_iam_role_policy" "read_tunnel_token" {
  name = "read-tunnel-token"
  role = aws_iam_role.sandbox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.tunnel_token_param}"
    }]
  })
}

resource "aws_iam_instance_profile" "sandbox" {
  name = "passageway-sandbox-host"
  role = aws_iam_role.sandbox.name
}

resource "aws_instance" "sandbox" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.sandbox.id]
  iam_instance_profile   = aws_iam_instance_profile.sandbox.name

  associate_public_ip_address = true

  # IMDSv2 only, hop limit 1 so containers can't reach instance metadata
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region             = var.region
    tunnel_token_param = var.tunnel_token_param
    compose            = file("${path.module}/host-compose.yml")
    caddyfile          = file("${path.module}/Caddyfile")
  })

  tags = { Name = "passageway-sandbox-host" }
}
