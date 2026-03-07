# Scaling Guide - Production Improvements

## Current State
- **Capacity**: 5-10 concurrent calls
- **Deployment**: Single Render Starter instance
- **State**: In-memory (not distributed)

## To Handle 50+ Users

### 1. Add Rate Limiting
```python
# Install: slowapi
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.post("/api/calls/outbound")
@limiter.limit("10/minute")  # Max 10 calls per minute per IP
async def initiate_outbound_call(req: OutboundCallRequest):
    ...
```

### 2. Add Redis for Distributed State
```python
# Install: redis, aioredis
import redis.asyncio as redis

# Store call context in Redis instead of memory
redis_client = redis.from_url("redis://...")

async def store_call_context(call_sid: str, context: CallContext):
    await redis_client.setex(
        f"call:{call_sid}", 
        3600,  # 1 hour TTL
        context.model_dump_json()
    )
```

### 3. Add Circuit Breakers
```python
# Install: circuitbreaker or pybreaker
from circuitbreaker import circuit

@circuit(failure_threshold=5, recovery_timeout=60)
async def fetch_dynamic_collection_flow():
    # Will stop calling API after 5 failures
    # Retry after 60 seconds
    ...
```

### 4. Add Monitoring
```python
# Install: sentry-sdk
import sentry_sdk
sentry_sdk.init(dsn="YOUR_SENTRY_DSN")

# Add prometheus metrics
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)
```

### 5. Upgrade Infrastructure

#### Render.com:
- **Web Service**: Upgrade to **Standard** ($25/mo) or **Pro** ($85/mo)
- **Database**: Upgrade to **Standard** ($20/mo) minimum
- **Add Redis**: Add Redis instance ($10/mo)

#### Alternative: AWS/GCP
- ECS/Cloud Run for auto-scaling
- RDS/Cloud SQL for database
- ElastiCache for Redis
- ALB/Load Balancer
- CloudWatch/Cloud Monitoring

### 6. Add Horizontal Scaling
```yaml
# render.yaml - Enable auto-scaling
services:
  - type: web
    name: aicallingagent
    plan: pro
    scaling:
      minInstances: 2
      maxInstances: 10
      targetCPUPercent: 70
```

### 7. Add Queue System
```python
# Install: celery, redis
# For async call processing
from celery import Celery

celery = Celery('tasks', broker='redis://...')

@celery.task
def process_call_async(call_data):
    # Process call in background
    ...
```

## Estimated Costs for Scale

| Users | Calls/Hour | Monthly Cost | Infrastructure |
|-------|------------|--------------|----------------|
| 10-50 | 100 | $50 | Starter + Standard DB |
| 50-200 | 500 | $150 | Standard + Redis |
| 200-1000 | 2000 | $400 | Pro + Redis + CDN |
| 1000+ | 10000+ | $1000+ | Multi-region + Auto-scale |

## Performance Optimization Checklist

- [ ] Enable Redis for session storage
- [ ] Add rate limiting (slowapi)
- [ ] Implement circuit breakers
- [ ] Add Sentry error tracking
- [ ] Set up Prometheus metrics
- [ ] Configure auto-scaling
- [ ] Add CDN for static assets
- [ ] Implement request queuing
- [ ] Add database read replicas
- [ ] Set up load balancer
- [ ] Configure graceful shutdown
- [ ] Add health check improvements
- [ ] Implement connection pooling tuning
- [ ] Add API request timeouts
- [ ] Configure CORS properly
- [ ] Set up log aggregation (DataDog/LogDNA)

## Quick Wins (No Code Changes)

1. **Upgrade Render Plan** → Standard ($25/mo)
   - 2GB RAM, 1 CPU
   - Handle 20-30 concurrent calls

2. **Update Environment Variables** in Render:
   - Set proper BASE_URL
   - Rotate all secrets
   - Use stronger encryption keys

3. **Enable Auto-Deploy** from main branch

4. **Set Up Alerts**:
   - Render dashboard → Enable email alerts
   - Set up UptimeRobot for monitoring

## Recommended Priority

### Phase 1 (Week 1):
- Upgrade to Standard plan
- Add rate limiting
- Set up Sentry

### Phase 2 (Week 2-3):
- Add Redis for state
- Implement circuit breakers
- Add monitoring

### Phase 3 (Month 2):
- Enable horizontal scaling
- Add queue system
- Optimize database queries
