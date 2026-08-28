# Arena tuning definition

Add a small compile-safe arena tuning definition for future difficulty work.

Create `Assets/Scripts/ArenaTuning.cs` as a public static class in the
`VerifiedGameBuilder.Game` namespace. It must expose
`public const int DifficultyTier = 2;` and must not modify existing files.
