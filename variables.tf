variable "region" {
  description = "AWS region for the sandbox host"
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = "AZ for the host. Must support the instance type (t3 is not offered in us-east-1e)"
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "EC2 size. t3.small is fine for testing; t3.medium for a handful of concurrent sessions"
  type        = string
  default     = "t3.medium"
}

variable "tunnel_token_param" {
  description = "SSM Parameter Store name holding the Cloudflare Tunnel token (create it manually, see SANDBOX-HOST-README.md)"
  type        = string
  default     = "/passageway/sandbox/tunnel_token"
}
