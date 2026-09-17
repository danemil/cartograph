pub fn rate_for(region: &str) -> f64 {
    if region == "eu" { 0.21 } else { 0.0 }
}

pub fn tax_on(amount: f64, region: &str) -> f64 {
    amount * rate_for(region)
}
