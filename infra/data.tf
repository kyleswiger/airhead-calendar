# Single-table design, per PRD §7. Every household entity - Member, Event, Source,
# MergeGroup, ReviewItem, AgentTurn - shares this table and is distinguished by the
# SK prefix, so an agenda read is one Query instead of a fan-out across tables.
#
# READ THIS BEFORE EDITING THE KEYS BELOW.
#
# A DynamoDB table's key schema and its GSIs' key schemas are immutable after
# creation. You cannot rename PK/SK, you cannot change a GSI's hash or range key,
# and you cannot repurpose an index - the only migration is a new table plus a
# full data copy, and with deletion protection on that is a deliberate, manual
# afternoon. Attributes, the TTL field, and non-key settings are all cheap to
# change later; the six key names here are not. They match the PRD exactly.
resource "aws_dynamodb_table" "airhead" {
  # Unprefixed on purpose: `airhead` IS the project prefix, and the PRD names the
  # table `airhead`. Renaming var.project would replace this table - see README.
  name         = var.project
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  # Household data with no other copy. Terraform must not be able to delete this
  # by accident; removal is a two-step where a human flips this off first.
  deletion_protection_enabled = true

  # Only key and index-key attributes are declared. Everything else on an item -
  # title, tier, rrule, contentHash - is schemaless and never appears here.
  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "GSI1PK"
    type = "S"
  }

  attribute {
    name = "GSI1SK"
    type = "S"
  }

  attribute {
    name = "GSI2PK"
    type = "S"
  }

  attribute {
    name = "GSI2SK"
    type = "S"
  }

  # GSI1 - external identity. `SRC#<sourceId>` / `EXT#<externalId>`. The sync path
  # looks an inbound provider event up here to decide upsert-vs-insert, so this is
  # the index that makes re-polling idempotent rather than duplicating the calendar.
  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  # GSI2 - per-member day slice. `HH#<hh>#MEM#<memberId>` / `<startUtc>`. The base
  # table is keyed by household, so "what is on Riley's Thursday" without this index
  # is a household-wide range scan filtered in the Lambda. ALL projection because
  # the kitchen view renders straight off the query result - a KEYS_ONLY index would
  # turn one Query into N GetItems on the hot read path.
  global_secondary_index {
    name            = "GSI2"
    hash_key        = "GSI2PK"
    range_key       = "GSI2SK"
    projection_type = "ALL"
  }

  # Wired now, used from M2. The agent turn log (PRD §7) and voice transcripts
  # (§12) expire themselves rather than needing a reaper Lambda. Items that set no
  # `ttl` attribute - every Member, Event, and Source - are simply never expired,
  # so enabling this ahead of the writer is inert. Turning TTL on later is a no-op
  # change; the key schema above is the part that had to be right on day one.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}
