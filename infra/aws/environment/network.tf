# VPC with three private tiers per availability zone:
#   service – ECS control plane, agents and interface endpoints
#   worker  – EC2 sandbox hosts
#   data    – managed broker, cache and database (no route beyond the VPC)
# Public subnets exist only to host the optional NAT gateway; nothing else lands there.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

# Remove every rule from the default security group so nothing uses it implicitly.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-default-unused" }
}

locals {
  # /16 -> /20 blocks: service 0-3, worker 4-7, data 8-11, public 12-15.
  subnet_bits = 4
  tiers       = { service = 0, worker = 4, data = 8 }
  private_subnets = merge([
    for tier, offset in local.tiers : {
      for index, az in local.azs : "${tier}-${index}" => {
        tier = tier
        az   = az
        cidr = cidrsubnet(var.vpc_cidr, local.subnet_bits, offset + index)
      }
    }
  ]...)
}

resource "aws_subnet" "private" {
  for_each                = local.private_subnets
  vpc_id                  = aws_vpc.main.id
  availability_zone       = each.value.az
  cidr_block              = each.value.cidr
  map_public_ip_on_launch = false
  tags = {
    Name = "${local.name}-${each.key}"
    Tier = each.value.tier
  }
}

locals {
  subnet_ids = {
    for tier in keys(local.tiers) : tier => [
      for index in range(var.az_count) : aws_subnet.private["${tier}-${index}"].id
    ]
  }
}

# --- Optional NAT egress -----------------------------------------------------

resource "aws_internet_gateway" "main" {
  count  = var.enable_nat_gateway ? 1 : 0
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = var.enable_nat_gateway ? 1 : 0
  vpc_id                  = aws_vpc.main.id
  availability_zone       = local.azs[0]
  cidr_block              = cidrsubnet(var.vpc_cidr, local.subnet_bits, 12)
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-public-nat", Tier = "public" }
}

resource "aws_route_table" "public" {
  count  = var.enable_nat_gateway ? 1 : 0
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main[0].id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.enable_nat_gateway ? 1 : 0
  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.name }
  depends_on    = [aws_internet_gateway.main]
}

# --- Private routing ----------------------------------------------------------

# Service and worker tiers: VPC-local, S3 via gateway endpoint, NAT only if enabled.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route" "private_nat" {
  count                  = var.enable_nat_gateway ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

# Data tier: no default route at all.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-data" }
}

resource "aws_route_table_association" "private" {
  for_each       = local.private_subnets
  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = each.value.tier == "data" ? aws_route_table.data.id : aws_route_table.private.id
}

# The data tier accepts and returns traffic only within the VPC.
resource "aws_network_acl" "data" {
  vpc_id     = aws_vpc.main.id
  subnet_ids = local.subnet_ids.data
  ingress {
    rule_no    = 100
    protocol   = "-1"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 0
    to_port    = 0
  }
  egress {
    rule_no    = 100
    protocol   = "-1"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 0
    to_port    = 0
  }
  tags = { Name = "${local.name}-data" }
}

# --- Endpoints ---------------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.data.id]
  tags              = { Name = "${local.name}-s3" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = local.endpoint_services
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = slice(local.subnet_ids.service, 0, var.endpoint_az_count)
  security_group_ids  = [aws_security_group.endpoints.id]
  tags                = { Name = "${local.name}-${each.value}" }
}

# --- Flow logs -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/${var.project}/${var.environment}/vpc-flow"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "flow" {
  name               = "${local.name}-vpc-flow"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
}

data "aws_iam_policy_document" "flow" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow.json
}

resource "aws_flow_log" "vpc" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = var.flow_log_traffic
  log_destination = aws_cloudwatch_log_group.flow.arn
  iam_role_arn    = aws_iam_role.flow.arn
}
