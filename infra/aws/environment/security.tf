# Security groups mirror the Compose topology. No group accepts traffic from outside
# the VPC; client ingress (transfer/submission API) is added with the control plane (T).

locals {
  ports = {
    grpc   = 50051 # coordinator intake/worker RPC and agent RPC
    health = 8080  # readiness and Prometheus scrape
    amqps  = 5671  # Amazon MQ for RabbitMQ (TLS)
    redis  = 6379  # ElastiCache (in-transit encryption)
    pg     = 5432  # RDS PostgreSQL
  }
  workload_groups = toset(["coordinator", "scheduler", "agents", "worker", "monitoring"])
}

resource "aws_security_group" "workload" {
  for_each    = local.workload_groups
  name        = "${local.name}-${each.key}"
  description = "${each.key} workload (${var.environment})"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-${each.key}" }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "data" {
  name        = "${local.name}-data"
  description = "Managed broker, cache and database (${var.environment})"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-data" }
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "Interface VPC endpoints (${var.environment})"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-endpoints" }
}

# --- Ingress -----------------------------------------------------------------

locals {
  ingress = concat(
    [
      for source in ["scheduler", "worker"] :
      { key = "coordinator-grpc-from-${source}", target = "coordinator", source = source, port = local.ports.grpc }
    ],
    [{ key = "agents-grpc-from-coordinator", target = "agents", source = "coordinator", port = local.ports.grpc }],
    [
      for target in ["coordinator", "scheduler", "agents", "worker"] :
      { key = "${target}-health-from-monitoring", target = target, source = "monitoring", port = local.ports.health }
    ],
  )
  data_clients = ["coordinator", "scheduler", "worker"]
  data_ingress = merge([
    for client in local.data_clients : {
      for name in ["amqps", "redis", "pg"] : "${name}-from-${client}" => {
        source = client
        port   = local.ports[name]
      }
    }
  ]...)
}

resource "aws_vpc_security_group_ingress_rule" "workload" {
  for_each                     = { for rule in local.ingress : rule.key => rule }
  security_group_id            = aws_security_group.workload[each.value.target].id
  referenced_security_group_id = aws_security_group.workload[each.value.source].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = each.key
}

resource "aws_vpc_security_group_ingress_rule" "data" {
  for_each                     = local.data_ingress
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.workload[each.value.source].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = each.key
}

resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  security_group_id = aws_security_group.endpoints.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS from inside the VPC"
}

# --- Egress ------------------------------------------------------------------

data "aws_ec2_managed_prefix_list" "s3" {
  name = "com.amazonaws.${var.region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "vpc" {
  for_each          = local.workload_groups
  security_group_id = aws_security_group.workload[each.key].id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 0
  to_port           = 65535
  description       = "TCP within the VPC (peers, data tier, endpoints)"
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  for_each          = local.workload_groups
  security_group_id = aws_security_group.workload[each.key].id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "S3 gateway endpoint (artifacts, ECR layers)"
}

# HTTPS to the internet exists only when a NAT gateway provides the path.
resource "aws_vpc_security_group_egress_rule" "https" {
  for_each          = var.enable_nat_gateway ? local.workload_groups : toset([])
  security_group_id = aws_security_group.workload[each.key].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS through the NAT gateway"
}
