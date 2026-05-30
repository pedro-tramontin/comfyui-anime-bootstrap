target "default" {
  dockerfile = "Dockerfile"
  platforms = ["linux/amd64"]
  tags = ["${GHCR_IMAGE}:latest"]
  cache-from = [
    "type=gha"
  ]
  cache-to = [
    "type=gha,mode=max"
  ]
}
