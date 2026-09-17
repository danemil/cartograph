package ledger

func Credit(balance int, amount int) int {
	return balance + amount
}

func Settle(balance int, amount int) int {
	return Credit(balance, -amount)
}
