output "instance_id" {
  value = aws_instance.sandbox.id
}

output "ssm_session_command" {
  description = "Open a shell on the host (no SSH needed)"
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.sandbox.id}"
}
