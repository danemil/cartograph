public class Account
{
    public int Deposit(int balance, int amount)
    {
        return balance + amount;
    }

    public int Transfer(int balance, int amount)
    {
        return Deposit(balance, -amount);
    }
}
