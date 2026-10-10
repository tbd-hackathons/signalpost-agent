//! Min-interval rate limiter shared by all tasks of one provider.
//!
//! Each `acquire` reserves the next free slot and sleeps until it. Slots are
//! reserved under a short lock and the sleep happens outside it, so waiters
//! queue in FIFO order of reservation and never burst.
//!
//! The limiter also carries a circuit breaker: when a provider asks us to stay
//! away for longer than we are willing to wait (for example a long
//! `Retry-After` after a quota is exhausted), `trip` opens the circuit and
//! `acquire` fails fast instead of sending more requests.

use std::sync::Mutex;
use std::time::Duration;
use tokio::time::Instant;

#[derive(Debug)]
pub struct RateLimiter {
    interval: Duration,
    next: Mutex<Option<Instant>>,
    open_until: Mutex<Option<Instant>>,
}

impl RateLimiter {
    pub fn new(interval: Duration) -> Self {
        Self {
            interval,
            next: Mutex::new(None),
            open_until: Mutex::new(None),
        }
    }

    /// Build from requests-per-second. Non-positive or non-finite values fall
    /// back to 1 rps.
    pub fn per_second(rps: f64) -> Self {
        let rps = if rps.is_finite() && rps > 0.0 {
            rps
        } else {
            1.0
        };
        Self::new(Duration::from_secs_f64(1.0 / rps))
    }

    pub fn interval(&self) -> Duration {
        self.interval
    }

    /// Wait for this caller's slot. Returns `false` without waiting while the
    /// circuit is open.
    pub async fn acquire(&self) -> bool {
        if self.is_open() {
            return false;
        }
        let wait = {
            let mut next = self.next.lock().unwrap_or_else(|p| p.into_inner());
            let now = Instant::now();
            let slot = match *next {
                Some(n) if n > now => n,
                _ => now,
            };
            *next = Some(slot + self.interval);
            slot - now
        };
        if !wait.is_zero() {
            tokio::time::sleep(wait).await;
        }
        !self.is_open()
    }

    /// Open the circuit for `duration`: no requests until it has passed.
    pub fn trip(&self, duration: Duration) {
        let mut g = self.open_until.lock().unwrap_or_else(|p| p.into_inner());
        let target = Instant::now() + duration;
        if g.is_none_or(|t| t < target) {
            *g = Some(target);
        }
    }

    pub fn is_open(&self) -> bool {
        let g = self.open_until.lock().unwrap_or_else(|p| p.into_inner());
        g.is_some_and(|t| Instant::now() < t)
    }

    /// Push the next free slot to at least `now + delay` for every waiter
    /// (used after a 429 or a provider's "slow down" response).
    pub fn penalize(&self, delay: Duration) {
        let mut next = self.next.lock().unwrap_or_else(|p| p.into_inner());
        let target = Instant::now() + delay;
        if next.is_none_or(|n| n < target) {
            *next = Some(target);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[tokio::test]
    async fn spaces_sequential_calls() {
        let rl = RateLimiter::new(Duration::from_millis(60));
        let start = Instant::now();
        for _ in 0..4 {
            assert!(rl.acquire().await);
        }
        // first call is immediate, then 3 intervals
        assert!(start.elapsed() >= Duration::from_millis(180));
    }

    #[tokio::test]
    async fn spaces_concurrent_calls() {
        let rl = Arc::new(RateLimiter::new(Duration::from_millis(50)));
        let start = Instant::now();
        let mut handles = Vec::new();
        for _ in 0..5 {
            let rl = rl.clone();
            handles.push(tokio::spawn(async move {
                rl.acquire().await;
                Instant::now()
            }));
        }
        let mut times = Vec::new();
        for h in handles {
            times.push(h.await.unwrap());
        }
        times.sort();
        // The guarantee: the i-th request never starts before start + i * interval.
        // (Gaps between wake-ups can look shorter when one task wakes late on a
        // busy machine; that is scheduler jitter, not a burst.)
        for (i, t) in times.iter().enumerate() {
            let earliest = start + Duration::from_millis(50 * i as u64);
            assert!(
                *t + Duration::from_millis(1) >= earliest,
                "request {i} started {:?} before its slot",
                earliest - *t
            );
        }
        assert!(start.elapsed() >= Duration::from_millis(200));
    }

    #[tokio::test]
    async fn penalize_delays_next_slot() {
        let rl = RateLimiter::new(Duration::from_millis(1));
        assert!(rl.acquire().await);
        rl.penalize(Duration::from_millis(120));
        let start = Instant::now();
        assert!(rl.acquire().await);
        assert!(start.elapsed() >= Duration::from_millis(110));
    }

    #[tokio::test]
    async fn tripped_circuit_fails_fast_then_closes() {
        let rl = RateLimiter::new(Duration::from_millis(1));
        rl.trip(Duration::from_millis(80));
        let start = Instant::now();
        assert!(!rl.acquire().await);
        assert!(start.elapsed() < Duration::from_millis(40));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(rl.acquire().await);
    }

    #[test]
    fn per_second_interval() {
        assert_eq!(
            RateLimiter::per_second(2.0).interval(),
            Duration::from_millis(500)
        );
        assert_eq!(
            RateLimiter::per_second(0.0).interval(),
            Duration::from_secs(1)
        );
        assert_eq!(
            RateLimiter::per_second(f64::NAN).interval(),
            Duration::from_secs(1)
        );
    }
}
