use clap::Parser;
use signalpost_search::config::{Provider, Settings};
use signalpost_search::runner::{run, RunOptions};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// Find candidate websites and news articles for companies.
///
/// Reads JSON Lines queries on stdin and writes JSON Lines results on stdout.
/// A JSON stats line is printed on stderr at the end. The Brave key is read
/// from the BRAVE_SEARCH_API_KEY environment variable only.
#[derive(Debug, Parser)]
#[command(name = "signalpost-search", version, about, long_about = None)]
struct Cli {
    /// Directory for the on-disk result cache. Caching is off when omitted.
    #[arg(long, value_name = "DIR")]
    cache_dir: Option<PathBuf>,

    /// Serve cached results younger than this many hours.
    #[arg(long, default_value_t = 168, value_name = "HOURS")]
    cache_ttl_hours: u64,

    /// Max in-flight requests per provider.
    #[arg(long, default_value_t = 4, value_parser = clap::value_parser!(u16).range(1..=64))]
    concurrency: u16,

    /// Comma-separated providers to query.
    #[arg(long, value_delimiter = ',', default_value = "brave,gdelt,wikipedia")]
    providers: Vec<Provider>,

    /// Per-request timeout in seconds.
    #[arg(long, default_value_t = 20, value_parser = clap::value_parser!(u64).range(1..=300))]
    timeout_secs: u64,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let mut providers = cli.providers.clone();
    providers.sort();
    providers.dedup();

    let opts = RunOptions {
        cache_dir: cli.cache_dir,
        cache_ttl: Duration::from_secs(cli.cache_ttl_hours.saturating_mul(3600)),
        concurrency: cli.concurrency as usize,
        providers,
        timeout: Duration::from_secs(cli.timeout_secs),
    };
    let settings = Settings::from_env();
    let usd_per_1000 = settings.brave_usd_per_1000;

    let rt = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("{}", serde_json::json!({"fatal": format!("runtime: {e}")}));
            return ExitCode::from(1);
        }
    };
    let started = Instant::now();
    let result = rt.block_on(async {
        let stdin = tokio::io::BufReader::new(tokio::io::stdin());
        run(opts, settings, stdin, tokio::io::stdout()).await
    });
    match result {
        Ok(stats) => {
            eprintln!(
                "{}",
                stats.to_json(usd_per_1000, started.elapsed().as_secs_f64())
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("{}", serde_json::json!({"fatal": e.to_string()}));
            ExitCode::from(1)
        }
    }
}
