export function vatFor(amount: number, rate: number): number {
  return amount * rate;
}

export function grossPrice(amount: number, rate: number): number {
  return amount + vatFor(amount, rate);
}
